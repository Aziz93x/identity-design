#!/usr/bin/env python3
"""Fetch only the identity guide declared in assets/downloads.json and verify it before publishing the local file."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import tempfile
from urllib.request import Request, urlopen

__author__ = "Abdulaziz Almalki"

SKILL_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = SKILL_ROOT / "assets" / "downloads.json"
BUNDLED_ROOT = SKILL_ROOT / "assets" / "identity"
ENVIRONMENT_VARIABLE = "IDENTITY_DESIGN_ROOT"
GUIDE_NAME = "ملف الهوية البصرية مفتوح (5).pdf"
GUIDE_URL = "https://github.com/Aziz93x/identity-design/releases/download/v1.3.0/identity-guide.pdf"
CHUNK_SIZE = 1024 * 1024
IS_WINDOWS = os.name == "nt"


def resolve_root(explicit_root=None, environ=None):
    environ = os.environ if environ is None else environ
    if explicit_root is not None:
        value, source = explicit_root, "--root"
    elif ENVIRONMENT_VARIABLE in environ:
        value, source = environ[ENVIRONMENT_VARIABLE], ENVIRONMENT_VARIABLE
    else:
        value, source = BUNDLED_ROOT, "bundled_assets"
    if not value or not str(value).strip():
        raise ValueError("The root selected by {} is empty; no fallback was attempted.".format(source))
    return Path(value).expanduser().resolve(), source


def load_guide(manifest_path):
    with Path(manifest_path).open("r", encoding="utf-8-sig") as stream:
        manifest = json.load(stream)
    records = manifest.get("downloads") if isinstance(manifest, dict) else None
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("downloads.json must contain exactly one downloads record for the guide.")
    record = dict(records[0])
    if record.get("id") != "guide" or record.get("url") != GUIDE_URL:
        raise ValueError("Only the guide at the fixed v1.3.0 GitHub release URL may be downloaded.")
    relative = record.get("path")
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("The guide path must be a nonempty relative file path.")
    portable = PurePosixPath(relative.replace("\\", "/"))
    if portable.is_absolute() or PureWindowsPath(relative).drive or ".." in portable.parts:
        raise ValueError("The guide path cannot be absolute or traverse outside the asset root.")
    if relative != GUIDE_NAME:
        raise ValueError("Only the named identity guide PDF may be prepared; no fonts or other assets are downloaded.")
    if type(record.get("bytes")) is not int or record["bytes"] <= 0:
        raise ValueError("The expected guide size must be a positive integer.")
    digest = record.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise ValueError("The guide must have a complete SHA-256 digest.")
    record["sha256"] = digest.lower()
    return record


def target_path(root, relative_path):
    destination = (root / relative_path).resolve()
    if root not in destination.parents:
        raise ValueError("The guide destination resolves outside the selected asset root.")
    return destination


def file_matches(path, record):
    if not path.is_file() or path.stat().st_size != record["bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest() == record["sha256"]


def publish_without_overwrite(temporary_path, destination):
    # Windows rename refuses an existing destination. POSIX rename replaces it,
    # so publish a same-filesystem hard link there and remove the temporary link.
    if IS_WINDOWS:
        os.rename(temporary_path, destination)
    else:
        os.link(temporary_path, destination)


def prepare_assets(explicit_root=None, manifest_path=None, opener=None, environ=None):
    root, root_source = resolve_root(explicit_root, environ)
    record = load_guide(MANIFEST if manifest_path is None else manifest_path)
    destination = target_path(root, record["path"])
    report = {
        "author": __author__, "status": "pass", "id": "guide",
        "root": str(root), "root_source": root_source, "path": str(destination),
        "bytes": record["bytes"], "sha256": record["sha256"],
    }
    if destination.exists() or destination.is_symlink():
        if file_matches(destination, record):
            return dict(report, action="already_present")
        raise ValueError("The existing guide does not match the expected size and SHA-256. It was left unchanged; choose another --root or resolve the existing file manually.")
    if root.exists() and not root.is_dir():
        raise ValueError("The selected asset root is not a directory.")
    root.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    open_url = urlopen if opener is None else opener
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".identity-guide-", suffix=".part", dir=root, delete=False) as output:
            temporary_path = Path(output.name)
            digest = hashlib.sha256()
            received = 0
            signature = b""
            request = Request(record["url"], headers={"User-Agent": "identity-design-assets/1.3.0"})
            with open_url(request, timeout=30) as response:
                while True:
                    chunk = response.read(min(CHUNK_SIZE, record["bytes"] - received + 1))
                    if not chunk:
                        break
                    signature = (signature + chunk)[:5]
                    if len(signature) == 5 and signature != b"%PDF-":
                        raise ValueError("The release response does not have a PDF signature.")
                    received += len(chunk)
                    if received > record["bytes"]:
                        raise ValueError("The download exceeds the expected guide size.")
                    digest.update(chunk)
                    output.write(chunk)
            if signature != b"%PDF-" or received != record["bytes"] or digest.hexdigest() != record["sha256"]:
                raise ValueError("Downloaded guide size or SHA-256 does not match downloads.json; no final file was created.")
            output.flush()
            os.fsync(output.fileno())
        # Publication must also refuse a file created while the download ran.
        try:
            publish_without_overwrite(temporary_path, destination)
        except FileExistsError as error:
            raise ValueError("A guide appeared at the destination during download. It was left unchanged; rerun to verify it.") from error
        return dict(report, action="downloaded")
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=("Root priority: --root, then IDENTITY_DESIGN_ROOT, then this skill's assets/identity directory on every OS. "
                "A missing selected directory is created as needed; there is no Windows legacy fallback, and an empty configured value fails. "
                "Only the declared guide is fetched from the fixed GitHub release URL. No fonts or other assets are downloaded. "
                "Existing matching files are skipped; mismatches are never overwritten. JSON output; exit 0 on success, 1 on failure, 2 on invalid command syntax."),
    )
    parser.add_argument("--root", metavar="PATH", help="Destination identity directory, overriding IDENTITY_DESIGN_ROOT and bundled assets/identity.")
    args = parser.parse_args(argv)
    try:
        report = prepare_assets(args.root)
        exit_code = 0
    except (OSError, ValueError, RuntimeError) as error:
        report = {"author": __author__, "status": "fail", "error": "{}: {}".format(type(error).__name__, error)}
        exit_code = 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
