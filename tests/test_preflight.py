"""Behavioral checks using temporary synthetic headers, never real identity assets."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

__author__ = "Abdulaziz Almalki"

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preflight.py"
SPEC = importlib.util.spec_from_file_location("identity_preflight", SCRIPT)
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="identity-preflight-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        bundle_patch = patch.object(PREFLIGHT, "BUNDLED_ROOT", self.root / "absent-bundle")
        bundle_patch.start()
        self.addCleanup(bundle_patch.stop)
        headers = {
            "text": "سياسة اختبار اصطناعية فقط".encode("utf-8"),
            "png": b"\x89PNG\r\n\x1a\nsynthetic test header",
            "pdf": b"%PDF-1.7\nsynthetic test header",
            "font": b"\x00\x01\x00\x00synthetic test header",
        }
        for relative_path, kind in PREFLIGHT.REQUIRED_ASSETS:
            target = self.root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(headers[kind])

    def test_explicit_root_wins_over_environment(self):
        root, source = PREFLIGHT.resolve_root(str(self.root), {PREFLIGHT.ENVIRONMENT_VARIABLE: "does-not-exist"}, "nt")
        self.assertEqual(root, self.root.resolve())
        self.assertEqual(source, "--root")

    def test_environment_wins_over_default(self):
        root, source = PREFLIGHT.resolve_root(None, {PREFLIGHT.ENVIRONMENT_VARIABLE: str(self.root)}, "nt")
        self.assertEqual(root, self.root.resolve())
        self.assertEqual(source, PREFLIGHT.ENVIRONMENT_VARIABLE)

    def test_default_exists_only_on_windows(self):
        root, source = PREFLIGHT.resolve_root(None, {}, "nt")
        self.assertEqual(str(root), str(Path(PREFLIGHT.WINDOWS_DEFAULT).resolve()))
        self.assertEqual(source, "windows_default")
        report, code = PREFLIGHT.preflight(None, {}, "posix")
        self.assertEqual(code, 2)
        self.assertEqual(report["checks"], [])
        self.assertIn("Windows-only", report["error"])

    def test_invalid_explicit_root_never_uses_valid_environment(self):
        missing = self.root / "missing"
        with patch.object(PREFLIGHT, "BUNDLED_ROOT", self.root):
            report, code = PREFLIGHT.preflight(str(missing), {PREFLIGHT.ENVIRONMENT_VARIABLE: str(self.root)}, "nt")
        self.assertEqual(code, 2)
        self.assertEqual(report["root"], str(missing.resolve()))
        self.assertEqual(report["root_source"], "--root")
        self.assertEqual(report["checks"], [])

    def test_invalid_environment_never_uses_valid_default(self):
        with patch.object(PREFLIGHT, "WINDOWS_DEFAULT", str(self.root)), patch.object(PREFLIGHT, "BUNDLED_ROOT", self.root):
            report, code = PREFLIGHT.preflight(None, {PREFLIGHT.ENVIRONMENT_VARIABLE: str(self.root / "missing")}, "nt")
        self.assertEqual(code, 2)
        self.assertEqual(report["root_source"], PREFLIGHT.ENVIRONMENT_VARIABLE)

    def test_bundle_is_used_on_windows_and_other_platforms(self):
        with patch.object(PREFLIGHT, "BUNDLED_ROOT", self.root), patch.object(PREFLIGHT, "WINDOWS_DEFAULT", str(self.root / "missing")):
            for platform in ("nt", "posix"):
                with self.subTest(platform=platform):
                    report, code = PREFLIGHT.preflight(None, {}, platform)
                    self.assertEqual(code, 0)
                    self.assertEqual(report["root"], str(self.root.resolve()))
                    self.assertEqual(report["root_source"], "bundled_assets")

    def test_configured_root_wins_over_existing_bundle(self):
        unused_bundle = self.root / "unused-bundle"
        unused_bundle.mkdir()
        with patch.object(PREFLIGHT, "BUNDLED_ROOT", unused_bundle):
            for explicit, environment, source in [(str(self.root), {}, "--root"), (None, {PREFLIGHT.ENVIRONMENT_VARIABLE: str(self.root)}, PREFLIGHT.ENVIRONMENT_VARIABLE)]:
                with self.subTest(source=source):
                    report, code = PREFLIGHT.preflight(explicit, environment, "posix")
                    self.assertEqual(code, 0)
                    self.assertEqual(report["root_source"], source)

    def test_incomplete_bundle_never_uses_complete_legacy_root(self):
        incomplete_bundle = self.root / "incomplete-bundle"
        incomplete_bundle.mkdir()
        with patch.object(PREFLIGHT, "BUNDLED_ROOT", incomplete_bundle), patch.object(PREFLIGHT, "WINDOWS_DEFAULT", str(self.root)):
            report, code = PREFLIGHT.preflight(None, {}, "nt")
        self.assertEqual(code, 1)
        self.assertEqual(report["root_source"], "bundled_assets")
        self.assertEqual(report["failed_count"], 9)

    def test_bundle_path_that_is_a_file_never_uses_legacy_root(self):
        bad_bundle = self.root / "bundle-file"
        bad_bundle.write_text("synthetic test", encoding="utf-8")
        with patch.object(PREFLIGHT, "BUNDLED_ROOT", bad_bundle), patch.object(PREFLIGHT, "WINDOWS_DEFAULT", str(self.root)):
            report, code = PREFLIGHT.preflight(None, {}, "nt")
        self.assertEqual(code, 2)
        self.assertEqual(report["root_source"], "bundled_assets")

    def test_empty_selected_values_do_not_fall_back(self):
        for explicit, environment in [("", {PREFLIGHT.ENVIRONMENT_VARIABLE: str(self.root)}), (None, {PREFLIGHT.ENVIRONMENT_VARIABLE: " "})]:
            with self.subTest(explicit=explicit, environment=environment):
                report, code = PREFLIGHT.preflight(explicit, environment, "nt")
                self.assertEqual(code, 2)
                self.assertIn("empty", report["error"])

    def test_complete_synthetic_fixture_passes_basic_checks(self):
        report, code = PREFLIGHT.preflight(str(self.root), {}, "posix")
        self.assertEqual(code, 0)
        self.assertEqual(report["checked_count"], 9)
        self.assertEqual(report["failed_count"], 0)
        self.assertTrue(all(row["status"] == "pass" for row in report["checks"]))

    def test_missing_asset_is_named_and_other_checks_continue(self):
        missing = PREFLIGHT.REQUIRED_ASSETS[2][0]
        (self.root / missing).unlink()
        report, code = PREFLIGHT.preflight(str(self.root), {}, "posix")
        self.assertEqual(code, 1)
        self.assertEqual(report["checked_count"], 9)
        failures = [row for row in report["checks"] if row["status"] == "fail"]
        self.assertEqual([row["relative_path"] for row in failures], [missing])

    def test_external_fonts_work_without_fonts_in_asset_root(self):
        external = self.root / "external-fonts"
        external.mkdir()
        for relative_path, kind in PREFLIGHT.REQUIRED_ASSETS:
            if kind == "font":
                (self.root / relative_path).rename(external / Path(relative_path).name)
        incomplete, code = PREFLIGHT.preflight(str(self.root), {}, "posix")
        self.assertEqual(code, 1)
        self.assertEqual(incomplete["failed_count"], 2)
        report, code = PREFLIGHT.preflight(str(self.root), {}, "posix", explicit_font_root=str(external))
        self.assertEqual(code, 0)
        self.assertEqual(report["checked_count"], 9)
        self.assertEqual(report["font_root_source"], "--font-root")
        self.assertEqual(report["font_root"], str(external.resolve()))

    def test_explicit_font_root_wins_over_environment(self):
        fonts = self.root / "الخطوط -Fonts"
        report, code = PREFLIGHT.preflight(str(self.root), {PREFLIGHT.FONT_ENVIRONMENT_VARIABLE: str(self.root / "missing")}, "posix", explicit_font_root=str(fonts))
        self.assertEqual(code, 0)
        self.assertEqual(report["font_root_source"], "--font-root")

    def test_invalid_explicit_font_root_never_uses_valid_environment(self):
        fonts = self.root / "الخطوط -Fonts"
        report, code = PREFLIGHT.preflight(str(self.root), {PREFLIGHT.FONT_ENVIRONMENT_VARIABLE: str(fonts)}, "posix", explicit_font_root=str(self.root / "missing"))
        self.assertEqual(code, 2)
        self.assertEqual(report["font_root_source"], "--font-root")
        self.assertEqual(report["checks"], [])

    def test_invalid_font_environment_does_not_use_asset_fonts(self):
        report, code = PREFLIGHT.preflight(str(self.root), {PREFLIGHT.FONT_ENVIRONMENT_VARIABLE: str(self.root / "missing")}, "nt")
        self.assertEqual(code, 2)
        self.assertEqual(report["font_root_source"], PREFLIGHT.FONT_ENVIRONMENT_VARIABLE)

    def test_empty_external_font_directory_does_not_use_asset_fonts(self):
        external = self.root / "external-empty"
        external.mkdir()
        report, code = PREFLIGHT.preflight(str(self.root), {}, "nt", explicit_font_root=str(external))
        self.assertEqual(code, 1)
        self.assertEqual(report["failed_count"], 2)
        self.assertEqual(report["font_root_source"], "--font-root")

    def test_font_environment_can_select_external_directory(self):
        fonts = self.root / "الخطوط -Fonts"
        report, code = PREFLIGHT.preflight(str(self.root), {PREFLIGHT.FONT_ENVIRONMENT_VARIABLE: str(fonts)}, "posix")
        self.assertEqual(code, 0)
        self.assertEqual(report["font_root_source"], PREFLIGHT.FONT_ENVIRONMENT_VARIABLE)

    def test_empty_configured_font_root_fails_without_fallback(self):
        for explicit, environment in [("", {}), (None, {PREFLIGHT.FONT_ENVIRONMENT_VARIABLE: " "})]:
            with self.subTest(explicit=explicit, environment=environment):
                report, code = PREFLIGHT.preflight(str(self.root), environment, "nt", explicit_font_root=explicit)
                self.assertEqual(code, 2)
                self.assertIn("empty", report["error"])

    def test_empty_and_wrong_binary_signatures_fail(self):
        for kind in ("png", "pdf", "font"):
            relative_path = next(rel for rel, item_kind in PREFLIGHT.REQUIRED_ASSETS if item_kind == kind)
            for content in (b"", b"not the expected file format"):
                with self.subTest(kind=kind, content=content):
                    (self.root / relative_path).write_bytes(content)
                    row = PREFLIGHT.check_asset(self.root, relative_path, kind)
                    self.assertEqual(row["status"], "fail")

    def test_supported_font_signatures(self):
        relative_path = next(rel for rel, kind in PREFLIGHT.REQUIRED_ASSETS if kind == "font")
        for signature in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            with self.subTest(signature=signature):
                (self.root / relative_path).write_bytes(signature + b"synthetic test header")
                self.assertEqual(PREFLIGHT.check_asset(self.root, relative_path, "font")["status"], "pass")

    def test_policy_text_requires_utf8_and_nonblank_content(self):
        relative_path = PREFLIGHT.REQUIRED_ASSETS[0][0]
        for content in (b"\xff\xfe\x00", b" \r\n\t", b"\xef\xbb\xbf"):
            with self.subTest(content=content):
                (self.root / relative_path).write_bytes(content)
                self.assertEqual(PREFLIGHT.check_asset(self.root, relative_path, "text")["status"], "fail")
        (self.root / relative_path).write_bytes(b"\xef\xbb\xbf" + "نص اختبار".encode("utf-8"))
        self.assertEqual(PREFLIGHT.check_asset(self.root, relative_path, "text")["status"], "pass")

    def test_pdf_read_is_bounded(self):
        class HeaderOnly(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 1024:
                    raise AssertionError("Binary preflight attempted an unbounded or large read")
                return super().read(size)

        relative_path = next(rel for rel, kind in PREFLIGHT.REQUIRED_ASSETS if kind == "pdf")
        with patch.object(Path, "open", return_value=HeaderOnly(b"%PDF-1.7\nsynthetic test header")):
            row = PREFLIGHT.check_asset(self.root, relative_path, "pdf")
        self.assertEqual(row["status"], "pass")

    def test_cli_returns_parseable_json_and_failure_exit(self):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        environment.pop(PREFLIGHT.FONT_ENVIRONMENT_VARIABLE, None)
        for directory, expected_code in [(self.root, 0), (self.root / "missing", 2)]:
            with self.subTest(directory=directory):
                result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--root", str(directory)], env=environment, capture_output=True, encoding="utf-8")
                self.assertEqual(result.returncode, expected_code, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["root_source"], "--root")
                self.assertEqual(report["status"], "pass" if expected_code == 0 else "fail")


if __name__ == "__main__":
    unittest.main()
