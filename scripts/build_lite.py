#!/usr/bin/env python3
"""Build a reproducible small installation ZIP from the validated source package."""

import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import quote
import zipfile

__author__ = "Abdulaziz Almalki"
ROOT = Path(__file__).resolve().parents[1]
FONTS = {
    "DINNextLTArabic-Regular-3.ttf",
    "alfont_com_AlFont_com_DINNextLTArabic-Medium.ttf",
    "DINNEXTLTARABIC-LIGHT-2-2.ttf",
}


def encoded_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def safe_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise ValueError("Non-portable package path: " + relative)
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Package path escapes its root: " + relative)
    return target


def keep_asset(record):
    relative = PurePosixPath(record["path"])
    return (
        record["group"].startswith("ready_")
        or (record["group"] == "logos" and relative.parent.as_posix().endswith("/PNG"))
        or (record["group"] == "fonts" and relative.name in FONTS)
    )


def build(root, output):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.is_relative_to(root):
        raise ValueError("Write the ZIP outside the source package.")
    if output.exists():
        raise ValueError("Output already exists; choose a new path to preserve it.")
    manifest = json.loads((root / "delivery-manifest.json").read_text(encoding="utf-8"))
    catalog = json.loads((root / "assets/catalog.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or catalog["version"] != version:
        raise ValueError("Manifest and catalog must declare the same release version.")
    records = {r["path"]: r for r in manifest["files"]}
    if len(records) != len(manifest["files"]):
        raise ValueError("Duplicate manifest paths.")
    selected_assets = {"assets/identity/" + r["path"] for r in catalog["files"] if keep_asset(r)}
    payload = {}
    original_bytes = 0
    for relative, record in sorted(records.items()):
        if record["distribution"] == "repository":
            original_bytes += record["bytes"]
        if relative.startswith("assets/identity/") and relative not in selected_assets:
            continue
        source = safe_path(root, relative)
        contents = source.read_bytes()
        if len(contents) != record["bytes"] or hashlib.sha256(contents).hexdigest() != record["sha256"]:
            raise ValueError("Source differs from its manifest: " + relative)
        payload[relative] = contents
    if not selected_assets.issubset(payload):
        raise ValueError("A selected catalog asset is missing from the manifest.")
    lite_catalog = copy.deepcopy(catalog)
    optional_assets = 0
    for record in lite_catalog["files"]:
        if record["availability"] == "bundled" and "assets/identity/" + record["path"] not in payload:
            record["availability"] = "repository_download"
            record["source_url"] = (
                "https://raw.githubusercontent.com/Aziz93x/identity-design/v" + version + "/assets/identity/"
                + quote(record["path"], safe="/")
            )
            optional_assets += 1
    lite_catalog["package_profile"] = "lite"
    payload["assets/catalog.json"] = encoded_json(lite_catalog)
    lite_manifest = copy.deepcopy(manifest)
    lite_manifest["package_profile"] = "lite"
    lite_manifest["release_distribution"] = "Small generated installation package; optional originals resolve to the pinned repository tag, and the guide retains its original release URL."
    lite_manifest["font_distribution"] = "Includes the three original DIN Arabic font files needed for the ready templates. Other original fonts remain available from the pinned repository tag; original licenses and attribution apply."
    lite_manifest["files"] = [
        {"path": relative, "bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest(), "distribution": "repository"}
        for relative, contents in sorted(payload.items())
    ]
    lite_manifest["files"].extend(copy.deepcopy(r) for r in manifest["files"] if r["distribution"] == "existing_release_download")
    lite_manifest["files"].sort(key=lambda r: r["path"])
    payload["delivery-manifest.json"] = encoded_json(lite_manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    created_output = False
    try:
        stream = output.open("xb")
        created_output = True
        with stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, contents in sorted(payload.items()):
                info = zipfile.ZipInfo("identity-design/" + relative, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, contents, compresslevel=9)
    except Exception:
        if created_output:
            output.unlink(missing_ok=True)
        raise
    return {
        "author": __author__, "version": version, "profile": "lite", "output": str(output),
        "files": len(payload), "bundled_identity_assets": len(selected_assets), "optional_repository_assets": optional_assets,
        "source_file_bytes_excluding_manifest": original_bytes,
        "lite_file_bytes_excluding_manifest": sum(len(v) for k, v in payload.items() if k != "delivery-manifest.json"),
        "zip_bytes": output.stat().st_size,
        "zip_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True, help="New ZIP path outside the package.")
    args = parser.parse_args(argv)
    try:
        report = build(args.root, args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"author": __author__, "status": "fail", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(dict(report, status="pass"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
