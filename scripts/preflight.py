#!/usr/bin/env python3
"""Check local Aldayer College identity assets without modifying or downloading them."""

import argparse
import json
import os
from pathlib import Path
import sys

__author__ = "Abdulaziz Almalki"

ENVIRONMENT_VARIABLE = "IDENTITY_DESIGN_ROOT"
FONT_ENVIRONMENT_VARIABLE = "IDENTITY_DESIGN_FONT_ROOT"
BUNDLED_ROOT = Path(__file__).resolve().parents[1] / "assets" / "identity"
REQUIRED_ASSETS = (
    ("الشعارات - Logos/PNG/horizontal-color.png", "png"),
    ("الشعارات - Logos/PNG/horizontal-white.png", "png"),
    ("الشعارات - Logos/PNG/vertical-color.png", "png"),
    ("الشعارات - Logos/PNG/vertical-white.png", "png"),
    ("الخطوط -Fonts/DINNextLTArabic-Regular-3.ttf", "font"),
    ("الخطوط -Fonts/alfont_com_AlFont_com_DINNextLTArabic-Medium.ttf", "font"),
    ("ملف الهوية البصرية مفتوح (5).pdf", "pdf"),
)
SIGNATURES = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "pdf": (b"%PDF-",),
    "font": (b"\x00\x01\x00\x00", b"OTTO", b"true"),
}


def resolve_root(explicit_root=None, environ=None):
    """Choose one root; an invalid explicit or environment value never falls back."""
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


def resolve_font_root(asset_root, explicit_font_root=None, environ=None):
    """Choose a font directory without falling back after a configured value."""
    environ = os.environ if environ is None else environ
    if explicit_font_root is not None:
        value, source = explicit_font_root, "--font-root"
    elif FONT_ENVIRONMENT_VARIABLE in environ:
        value, source = environ[FONT_ENVIRONMENT_VARIABLE], FONT_ENVIRONMENT_VARIABLE
    else:
        value, source = asset_root / "الخطوط -Fonts", "asset_root"
    if not value or not str(value).strip():
        raise ValueError("The font root selected by {} is empty; no fallback was attempted.".format(source))
    return Path(value).expanduser().resolve(), source


def check_asset(root, relative_path, kind):
    path = root / relative_path
    row = {"relative_path": relative_path, "kind": kind, "status": "fail"}
    try:
        if not path.is_file():
            row["error"] = "Missing file or path is not a regular file."
            return row
        row["size_bytes"] = path.stat().st_size
        if row["size_bytes"] == 0:
            row["error"] = "File is empty."
            return row
        with path.open("rb") as stream:
            # Inspect only the header, including for the large identity PDF.
            header = stream.read(16)
            if not any(header.startswith(signature) for signature in SIGNATURES[kind]):
                row["error"] = "Unexpected {} file signature.".format(kind.upper())
                return row
        row["status"] = "pass"
    except (OSError, ValueError) as error:
        row["error"] = "Cannot read asset: {}: {}".format(type(error).__name__, error)
    return row


def preflight(explicit_root=None, environ=None, explicit_font_root=None):
    report = {
        "author": __author__,
        "status": "fail",
        "root": None,
        "root_source": None,
        "font_root": None,
        "font_root_source": None,
        "checks": [],
        "limitations": "Checks seven basic identity files and their signatures: four logos, two fonts, and the guide. It does not verify brand approval, font loading, complete file validity, or rendered output. Templates and optional assets must be checked when the task uses them.",
    }
    try:
        root, source = resolve_root(explicit_root, environ)
        report.update(root=str(root), root_source=source)
        if not root.is_dir():
            report["error"] = "Selected identity root is missing or is not a directory. Provide the assets at this path or select an existing root with --root or IDENTITY_DESIGN_ROOT. No fallback, copying or downloading was attempted."
            return report, 2
        font_root, font_source = resolve_font_root(root, explicit_font_root, environ)
        report.update(font_root=str(font_root), font_root_source=font_source)
        if font_source != "asset_root" and not font_root.is_dir():
            report["error"] = "Configured font root is missing or is not a directory. No fallback to bundled or installed fonts was attempted."
            return report, 2
    except (OSError, ValueError, RuntimeError) as error:
        report["error"] = str(error)
        return report, 2
    for relative_path, kind in REQUIRED_ASSETS:
        selected_root = font_root if kind == "font" else root
        selected_path = Path(relative_path).name if kind == "font" else relative_path
        row = check_asset(selected_root, selected_path, kind)
        row["root"] = str(selected_root)
        report["checks"].append(row)
    report["checked_count"] = len(report["checks"])
    report["failed_count"] = sum(row["status"] != "pass" for row in report["checks"])
    report["status"] = "pass" if report["failed_count"] == 0 else "fail"
    return report, 0 if report["status"] == "pass" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=("Root priority on every OS: --root, then IDENTITY_DESIGN_ROOT, then the skill's bundled assets/identity. "
                "An invalid selected root or incomplete bundle never falls back. "
                "Font priority: --font-root, then IDENTITY_DESIGN_FONT_ROOT, then the selected asset root's 'الخطوط -Fonts' directory. "
                "Regular and Medium are required by this check; Light is checked separately when a task uses it. "
                "Output is JSON. Exit codes: 0 = checks passed, 1 = asset checks failed, 2 = root/usage error. "
                "This helper uses the Python standard library and does not install fonts or create files."),
    )
    parser.add_argument("--root", metavar="PATH", help="Local identity asset directory, overriding IDENTITY_DESIGN_ROOT.")
    parser.add_argument("--font-root", metavar="PATH", help="Directory containing the original named DIN font files, overriding IDENTITY_DESIGN_FONT_ROOT.")
    args = parser.parse_args(argv)
    report, exit_code = preflight(args.root, explicit_font_root=args.font_root)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
