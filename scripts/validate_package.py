#!/usr/bin/env python3
"""Verify the package inventory, asset hashes, download metadata and local Markdown file links."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

__author__ = "Abdulaziz Almalki"

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "delivery-manifest.json"
IGNORED_DIRECTORIES = {".git", "__pycache__", ".venv"}
IGNORED_FILES = {".git", ".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".part", ".download"}
HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")
# Package documentation uses inline/angle links and reference definitions.
# One level of balanced parentheses accommodates filenames such as guide (5).pdf.
LINK = re.compile(r"\]\(\s*(?:<([^>]+)>|((?:\\.|[^\s()\\]|\([^()]*\))+))(?:\s+[^\n]*?)?\s*\)")
REFERENCE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*(?:<([^>]+)>|(\S+))", re.MULTILINE)


def safe_path(root, value):
    """Accept portable package-relative paths and reject escaping symlinks."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("Invalid portable relative path: {!r}".format(value))
    relative = PurePosixPath(value)
    if relative.is_absolute() or PureWindowsPath(value).drive or ".." in relative.parts or str(relative) != value:
        raise ValueError("Invalid portable relative path: {!r}".format(value))
    path = root.joinpath(*relative.parts).resolve()
    if root not in path.parents:
        raise ValueError("Path escapes package root: {}".format(value))
    return path


def read_json(path):
    with path.open(encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("{} must contain a JSON object".format(path.name))
    return value


def records_by_path(value, label, root):
    if not isinstance(value, list):
        raise ValueError("{} must be a list".format(label))
    records, portable_names = {}, set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("{} contains a non-object record".format(label))
        name = row.get("path")
        safe_path(root, name)
        if name.casefold() in portable_names:
            raise ValueError("{} contains a duplicate or case-colliding path: {}".format(label, name))
        if type(row.get("bytes")) is not int or row["bytes"] < 0:
            raise ValueError("{}: invalid byte count for {}".format(label, name))
        if not isinstance(row.get("sha256"), str) or HEX_DIGEST.fullmatch(row["sha256"]) is None:
            raise ValueError("{}: invalid SHA-256 for {}".format(label, name))
        records[name] = row
        portable_names.add(name.casefold())
    return records


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def same_content(left, right):
    return left["bytes"] == right["bytes"] and left["sha256"].lower() == right["sha256"].lower()


def package_files(root):
    names = set()
    for directory, children, files in os.walk(root):
        children[:] = sorted(name for name in children if name not in IGNORED_DIRECTORIES)
        for name in children + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("Package symlinks are not supported: {}".format(path.relative_to(root).as_posix()))
        for name in files:
            path = Path(directory) / name
            if name not in IGNORED_FILES and path.suffix not in IGNORED_SUFFIXES:
                names.add(path.relative_to(root).as_posix())
    return names


def metadata(root):
    manifest = read_json(root / MANIFEST_NAME)
    catalog = read_json(root / "assets/catalog.json")
    downloads = read_json(root / "assets/downloads.json")
    entries = records_by_path(manifest.get("files"), "manifest files", root)
    if not isinstance(catalog.get("asset_root", "identity"), str):
        raise ValueError("catalog asset_root must be a relative path string")
    asset_root = safe_path(root, "assets/" + catalog.get("asset_root", "identity"))
    assets = records_by_path(catalog.get("files"), "catalog files", asset_root)
    download_records = records_by_path(downloads.get("downloads"), "downloads", asset_root)
    if len(download_records) != 1 or next(iter(download_records.values())).get("id") != "guide":
        raise ValueError("downloads must declare exactly one optional guide")
    for row in download_records.values():
        url = urlsplit(row.get("url") if isinstance(row.get("url"), str) else "")
        if url.scheme != "https" or not url.netloc or row["bytes"] == 0:
            raise ValueError("The guide must have an HTTPS URL and a positive byte count")
    prefix = asset_root.relative_to(root).as_posix() + "/"
    return manifest, entries, assets, download_records, prefix


def markdown_links(root, names, optional_paths):
    errors, checked = [], 0
    for name in sorted(names):
        if not name.lower().endswith(".md"):
            continue
        source = safe_path(root, name)
        content = source.read_text(encoding="utf-8-sig")
        content = re.sub(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[^\n]*$", "", content, flags=re.MULTILINE | re.DOTALL)
        content = re.sub(r"`[^`\n]*`", "", content)
        for match in list(LINK.finditer(content)) + list(REFERENCE.finditer(content)):
            target = re.sub(r"\\([\\()<> ])", r"\1", match.group(1) or match.group(2))
            parts = urlsplit(target)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            checked += 1
            target_path = (source.parent / unquote(parts.path)).resolve()
            if target_path != root and root not in target_path.parents:
                errors.append("{}: local link escapes package: {}".format(name, target))
            elif not target_path.exists() and target_path.relative_to(root).as_posix() not in optional_paths:
                errors.append("{}: broken local file link: {}".format(name, target))
    return checked, errors


def validate(root):
    root = Path(root).resolve()
    manifest, entries, assets, downloads, prefix = metadata(root)
    errors, checked, absent, external_absent = [], 0, [], []
    optional = {prefix + name for name in downloads}
    external = {prefix + name for name, row in assets.items() if row.get("availability") == "repository_download"}
    verified_external = set()
    if MANIFEST_NAME in entries:
        errors.append("The manifest must not hash itself")
    for name, row in entries.items():
        path = safe_path(root, name)
        if name in optional and not path.exists():
            absent.append(name)
            continue
        if not path.is_file():
            errors.append("Missing manifest file: {}".format(name))
        elif not same_content(row, fingerprint(path)):
            errors.append("Manifest size/SHA-256 mismatch: {}".format(name))
        else:
            checked += 1
    for name, row in assets.items():
        package_name = prefix + name
        external_record = package_name in external
        if external_record:
            repository = urlsplit(manifest.get("repository") if isinstance(manifest.get("repository"), str) else "")
            source = urlsplit(row.get("source_url") if isinstance(row.get("source_url"), str) else "")
            repo_parts = repository.path.strip("/").split("/")
            source_prefix = repository.path.rstrip("/") + "/"
            source_parts = unquote(source.path[len(source_prefix):]).split("/", 1)
            if (repository.scheme != "https" or repository.netloc != "github.com" or len(repo_parts) != 2
                    or source.scheme != "https" or source.netloc != "raw.githubusercontent.com"
                    or not source.path.startswith(source_prefix) or len(source_parts) != 2
                    or not source_parts[0] or source_parts[-1] != package_name or source.query or source.fragment):
                errors.append("Repository download needs an HTTPS raw source in the declared repository: {}".format(name))
            asset_path = safe_path(root, package_name)
            if not asset_path.exists():
                external_absent.append(package_name)
            elif not asset_path.is_file() or not same_content(row, fingerprint(asset_path)):
                errors.append("Repository asset size/SHA-256 mismatch: {}".format(package_name))
            else:
                verified_external.add(package_name)
        if package_name not in entries and not external_record:
            errors.append("Catalog asset missing from manifest: {}".format(package_name))
        elif package_name in entries and not same_content(row, entries[package_name]):
            errors.append("Catalog/manifest size/SHA-256 mismatch: {}".format(package_name))
        source_digest = row.get("source_sha256")
        if row.get("preserved_original") is True and (not isinstance(source_digest, str) or source_digest.lower() != row["sha256"].lower()):
            errors.append("Preserved original differs from source SHA-256: {}".format(package_name))
        if row.get("availability") == "release_download" and name not in downloads:
            errors.append("Catalog download is not declared in downloads.json: {}".format(name))
    for name in entries:
        if name.startswith(prefix) and name[len(prefix):] not in assets:
            errors.append("Manifest asset missing from catalog: {}".format(name))
    for name, row in downloads.items():
        catalog_row = assets.get(name)
        if catalog_row is None or not same_content(row, catalog_row) or row["url"] != catalog_row.get("url"):
            errors.append("Download/catalog metadata mismatch: {}".format(name))
        elif catalog_row.get("availability") != "release_download":
            errors.append("Guide must be marked release_download in catalog: {}".format(name))
        entry = entries.get(prefix + name)
        if entry is None or entry.get("distribution") not in ("release_download", "existing_release_download"):
            errors.append("Guide must be declared as a release download in manifest: {}".format(name))
    actual = package_files(root)
    for name in sorted(actual - set(entries) - verified_external - {MANIFEST_NAME}):
        errors.append("Unmanifested package file: {}".format(name))
    link_count, link_errors = markdown_links(root, actual, optional | external)
    errors.extend(link_errors)
    return {"author": __author__, "status": "fail" if errors else "pass", "root": str(root),
            "manifest_files_verified": checked, "catalog_assets": len(assets),
            "optional_downloads_absent": absent, "local_markdown_links_checked": link_count,
            "optional_repository_assets_absent": external_absent,
            "errors": errors,
            "limitations": "Checks declared bytes, inventory and local Markdown file targets; does not render assets, validate heading anchors or fetch external links."}


def write_manifest(root):
    """Refresh declared/tracked files only, preserving release metadata and optional downloads."""
    root = Path(root).resolve()
    # Reject package symlinks before any writes, including a manifest link that
    # would otherwise follow its target outside this package during write_text.
    package_files(root)
    manifest_path = safe_path(root, MANIFEST_NAME)
    manifest, entries, _, downloads, prefix = metadata(root)
    names = set(entries)
    # In a checkout, include new intentionally staged files, never untracked extras.
    if (root / ".git").exists():
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
        names.update(name for name in result.stdout.decode("utf-8").split("\x00") if name)
    names.discard(MANIFEST_NAME)
    optional = {prefix + name: row for name, row in downloads.items()}
    records = []
    for name in sorted(names):
        path = safe_path(root, name)
        record = dict(entries.get(name, {}), path=name)
        if name in optional:
            record.update(bytes=optional[name]["bytes"], sha256=optional[name]["sha256"])
            record["distribution"] = "existing_release_download"
            if path.exists() and not same_content(record, fingerprint(path)):
                raise ValueError("Existing guide differs from downloads.json: {}".format(name))
        else:
            record.update(fingerprint(path))
            record.setdefault("distribution", "repository")
        records.append(record)
    manifest["files"] = records
    # All source reads must succeed before updating the manifest.
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Default validation is read-only and offline. Missing declared guide downloads are allowed; present copies must match. "
        "--write-manifest refreshes existing and Git-tracked file entries, then validates. Stage intended new files first. "
        "Top-level release metadata is preserved. Git/Python caches, OS metadata and .part/.download files are excluded. "
        "Exit 0 means pass, 1 means failure, 2 means invalid command syntax."))
    parser.add_argument("--root", type=Path, default=PACKAGE_ROOT, help="Package directory, not the identity asset directory.")
    parser.add_argument("--write-manifest", action="store_true", help="Refresh manifest bytes/hashes before validating; never updates catalog hashes.")
    args = parser.parse_args(argv)
    try:
        if args.write_manifest:
            write_manifest(args.root)
        report = validate(args.root)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report = {"author": __author__, "status": "fail", "errors": ["{}: {}".format(type(error).__name__, error)]}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
