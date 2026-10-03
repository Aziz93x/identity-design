"""Offline package verification using small, disposable package fixtures."""

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

__author__ = "Abdulaziz Almalki"

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_package.py"
SPEC = importlib.util.spec_from_file_location("identity_validate_package", SCRIPT)
VALIDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATE)


class ValidatePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="identity-package-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.assets_root = self.root / "assets/identity"
        self.assets_root.mkdir(parents=True)
        self.logo = b"synthetic-logo"
        self.guide = b"%PDF-1.7\nSynthetic guide\n"
        self.asset_name = "شعار (1).png"
        self.guide_name = "guide (5).pdf"
        self.logo_record = dict(self.record(self.asset_name, self.logo), availability="bundled",
                                preserved_original=True, source_sha256=hashlib.sha256(self.logo).hexdigest())
        self.guide_record = dict(self.record(self.guide_name, self.guide), id="guide",
                                 url="https://github.com/example/identity/releases/download/v1/guide.pdf")
        self.catalog = {"asset_root": "identity", "files": [self.logo_record,
                        dict(self.guide_record, availability="release_download")]}
        self.downloads = {"downloads": [self.guide_record]}
        self.put("assets/identity/" + self.asset_name, self.logo)
        self.put("README.md", "[asset](<assets/identity/شعار (1).png>)\n[guide](<assets/identity/guide (5).pdf>)\n")
        self.sync_metadata()
        self.manifest = {"schema": "identity-design-delivery/v1", "author": __author__,
                         "version": "test", "repository": "https://github.com/example/identity",
                         "validation": {"preserve_me": True}, "files": []}
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                self.manifest["files"].append(dict(path=path.relative_to(self.root).as_posix(),
                                                   distribution="repository", **VALIDATE.fingerprint(path)))
        self.manifest["files"].append(dict(self.guide_record, path="assets/identity/" + self.guide_name,
                                           distribution="existing_release_download"))
        self.save_manifest()

    @staticmethod
    def record(name, data):
        return {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)

    def save_manifest(self):
        self.put(VALIDATE.MANIFEST_NAME, json.dumps(self.manifest, ensure_ascii=False))

    def sync_metadata(self):
        self.put("assets/catalog.json", json.dumps(self.catalog, ensure_ascii=False))
        self.put("assets/downloads.json", json.dumps(self.downloads, ensure_ascii=False))

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            result = VALIDATE.main(["--root", str(self.root), *args])
        return result, json.loads(output.getvalue())

    def assert_failure(self, expected):
        code, report = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "fail")
        self.assertIn(expected, "\n".join(report["errors"]))
        return report

    def test_real_fixture_passes_with_optional_guide_absent(self):
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["optional_downloads_absent"], ["assets/identity/" + self.guide_name])
        self.assertEqual(report["local_markdown_links_checked"], 2)

    def test_present_guide_is_verified(self):
        self.put("assets/identity/" + self.guide_name, self.guide)
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["optional_downloads_absent"], [])
        self.put("assets/identity/" + self.guide_name, b"x" * len(self.guide))
        self.assert_failure("Manifest size/SHA-256 mismatch")

    def test_missing_asset_and_same_size_tampering_fail(self):
        self.put("assets/identity/" + self.asset_name, b"x" * len(self.logo))
        self.assert_failure("Manifest size/SHA-256 mismatch")
        (self.assets_root / self.asset_name).unlink()
        self.assert_failure("Missing manifest file")

    def test_catalog_hash_disagreement_is_not_hidden_by_manifest_refresh(self):
        self.put("assets/identity/" + self.asset_name, b"changed asset")
        code, report = self.run_cli("--write-manifest")
        self.assertEqual(code, 1)
        self.assertIn("Catalog/manifest size/SHA-256 mismatch", "\n".join(report["errors"]))

    def test_download_metadata_must_match_catalog_and_manifest(self):
        self.downloads["downloads"][0] = dict(self.guide_record, sha256="0" * 64)
        self.sync_metadata()
        self.assert_failure("Download/catalog metadata mismatch")

    def test_new_untracked_file_is_rejected_but_runtime_files_are_ignored(self):
        for name in ("__pycache__/helper.pyc", "cache.part", "cache.download", ".DS_Store", ".git/config"):
            self.put(name, b"runtime")
        self.assertEqual(self.run_cli()[0], 0)
        self.put("unintended.txt", "should not enter a release")
        self.assert_failure("Unmanifested package file: unintended.txt")

    def test_git_worktree_pointer_is_not_a_package_file(self):
        self.put(".git", "gitdir: ../parent/.git/worktrees/test\n")
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)

    def test_local_markdown_links_decode_paths_and_skip_examples(self):
        self.put("README.md", "[logo](assets/identity/%D8%B4%D8%B9%D8%A7%D8%B1%20%281%29.png)\n"
                 "[reference][guide]\n[guide]: <assets/identity/guide (5).pdf>\n"
                 "[web](https://example.com/absent)\n[anchor](#section)\n"
                 "```md\n[example](missing.md)\n```\n`[inline example](missing.md)`\n")
        code, report = self.run_cli("--write-manifest")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["local_markdown_links_checked"], 2)
        self.put("README.md", "[broken](missing.md)\n")
        self.assert_failure("broken local file link")

    def test_local_link_cannot_escape_package(self):
        self.put("README.md", "[outside](../outside.md)\n")
        self.assert_failure("local link escapes package")

    def test_invalid_paths_hashes_and_duplicate_paths_fail_as_json(self):
        original = dict(self.manifest["files"][0])
        for changes in ({"path": "../escape"}, {"path": "C:/escape"}, {"path": "nested\\file"},
                        {"bytes": True}, {"sha256": "invalid"}):
            with self.subTest(changes=changes):
                self.manifest["files"][0] = dict(original, **changes)
                self.save_manifest()
                self.assertEqual(self.run_cli()[0], 1)
        self.manifest["files"][0] = original
        self.manifest["files"].append(dict(original, path=original["path"].upper()))
        self.save_manifest()
        self.assert_failure("duplicate or case-colliding")

    def test_refresh_preserves_release_metadata_and_does_not_admit_untracked_files(self):
        self.put("README.md", "Updated documentation\n")
        self.put("accidental.txt", "not staged")
        code, report = self.run_cli("--write-manifest")
        self.assertEqual(code, 1)
        self.assertIn("Unmanifested package file: accidental.txt", report["errors"])
        refreshed = VALIDATE.read_json(self.root / VALIDATE.MANIFEST_NAME)
        self.assertEqual(refreshed["version"], "test")
        self.assertEqual(refreshed["validation"], {"preserve_me": True})
        self.assertNotIn("accidental.txt", [row["path"] for row in refreshed["files"]])
        readme = next(row for row in refreshed["files"] if row["path"] == "README.md")
        self.assertEqual(readme["distribution"], "repository")
        self.assertTrue(VALIDATE.same_content(readme, VALIDATE.fingerprint(self.root / "README.md")))

    def test_refresh_includes_git_tracked_new_file(self):
        self.put(".git/config", "synthetic")
        self.put("new.py", "# Author: Abdulaziz Almalki\n")
        completed = subprocess.CompletedProcess([], 0, stdout=b"new.py\x00delivery-manifest.json\x00")
        with patch.object(VALIDATE.subprocess, "run", return_value=completed) as git:
            code, report = self.run_cli("--write-manifest")
        self.assertEqual(code, 0, report)
        git.assert_called_once()
        refreshed = VALIDATE.read_json(self.root / VALIDATE.MANIFEST_NAME)
        self.assertIn("new.py", [row["path"] for row in refreshed["files"]])

    def test_failed_refresh_leaves_manifest_unchanged(self):
        before = (self.root / VALIDATE.MANIFEST_NAME).read_bytes()
        (self.root / "README.md").unlink()
        code, _ = self.run_cli("--write-manifest")
        self.assertEqual(code, 1)
        self.assertEqual((self.root / VALIDATE.MANIFEST_NAME).read_bytes(), before)

    def test_refresh_rejects_manifest_symlink_before_changing_outside_file(self):
        outside_directory = tempfile.TemporaryDirectory(prefix="identity-outside-test-")
        self.addCleanup(outside_directory.cleanup)
        outside = Path(outside_directory.name) / "outside-manifest.json"
        manifest_path = self.root / VALIDATE.MANIFEST_NAME
        original = manifest_path.read_bytes()
        outside.write_bytes(original)
        manifest_path.unlink()
        try:
            manifest_path.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest("This environment cannot create file symlinks: {}".format(error))
        self.put("README.md", "Changed documentation must not update outside metadata\n")
        code, report = self.run_cli("--write-manifest")
        self.assertEqual(code, 1)
        self.assertIn("Package symlinks are not supported", "\n".join(report["errors"]))
        self.assertEqual(outside.read_bytes(), original)

    def test_refresh_rejects_other_package_symlinks_before_changing_manifest(self):
        before = (self.root / VALIDATE.MANIFEST_NAME).read_bytes()
        try:
            (self.root / "linked.md").symlink_to(self.root / "README.md")
        except (OSError, NotImplementedError) as error:
            self.skipTest("This environment cannot create file symlinks: {}".format(error))
        self.put("README.md", "Changed documentation\n")
        code, report = self.run_cli("--write-manifest")
        self.assertEqual(code, 1)
        self.assertIn("Package symlinks are not supported", "\n".join(report["errors"]))
        self.assertEqual((self.root / VALIDATE.MANIFEST_NAME).read_bytes(), before)

    def test_preserved_original_flag_requires_source_hash_match(self):
        self.catalog["files"][0]["source_sha256"] = "f" * 64
        self.sync_metadata()
        self.assert_failure("Preserved original differs")

    def make_lite_asset(self):
        self.catalog["files"][0].update(availability="repository_download",
            source_url="https://raw.githubusercontent.com/example/identity/v1/" + quote("assets/identity/" + self.asset_name))
        (self.assets_root / self.asset_name).unlink()
        self.manifest["files"] = [row for row in self.manifest["files"] if row["path"] != "assets/identity/" + self.asset_name]
        self.sync_metadata()
        self.save_manifest()

    def test_lite_catalog_may_declare_absent_repository_asset(self):
        self.make_lite_asset()
        code, report = self.run_cli("--write-manifest")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["optional_repository_assets_absent"], ["assets/identity/" + self.asset_name])
        self.put("assets/identity/" + self.asset_name, b"x" * len(self.logo))
        self.assert_failure("Repository asset size/SHA-256 mismatch")

    def test_lite_downloaded_repository_asset_passes_without_manifest_entry(self):
        self.make_lite_asset()
        self.assertEqual(self.run_cli("--write-manifest")[0], 0)
        self.put("assets/identity/" + self.asset_name, self.logo)
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["optional_repository_assets_absent"], [])
        # A downloaded archive has no Git checkout and needs no inventory rewrite.
        self.assertFalse((self.root / ".git").exists())
        code, report = self.run_cli("--write-manifest")
        self.assertEqual(code, 0, report)
        refreshed = VALIDATE.read_json(self.root / VALIDATE.MANIFEST_NAME)
        self.assertNotIn("assets/identity/" + self.asset_name, [row["path"] for row in refreshed["files"]])

    def test_lite_optional_asset_rejects_conflicting_explicit_manifest_entry(self):
        self.make_lite_asset()
        self.assertEqual(self.run_cli("--write-manifest")[0], 0)
        self.put("assets/identity/" + self.asset_name, self.logo)
        self.manifest = VALIDATE.read_json(self.root / VALIDATE.MANIFEST_NAME)
        self.manifest["files"].append(dict(self.logo_record,
            path="assets/identity/" + self.asset_name, sha256="0" * 64, distribution="repository"))
        self.save_manifest()
        self.assert_failure("Catalog/manifest size/SHA-256 mismatch")

    def test_lite_repository_asset_requires_matching_repository_source(self):
        self.make_lite_asset()
        self.catalog["files"][0]["source_url"] = "https://raw.githubusercontent.com/someone/else/v1/logo.png"
        self.sync_metadata()
        self.assert_failure("Repository download needs an HTTPS raw source")

    def test_lite_repository_source_must_reference_the_declared_asset(self):
        self.make_lite_asset()
        self.catalog["files"][0]["source_url"] = "https://raw.githubusercontent.com/example/identity/v1/wrong.png"
        self.sync_metadata()
        self.assert_failure("Repository download needs an HTTPS raw source")

    def test_invalid_json_returns_json_failure(self):
        self.put("assets/catalog.json", "{invalid")
        self.assert_failure("JSONDecodeError")


if __name__ == "__main__":
    unittest.main()
