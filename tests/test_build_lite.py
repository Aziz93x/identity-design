import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location("build_lite", Path(__file__).resolve().parents[1] / "scripts/build_lite.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class LitePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.catalog = {"version": "1.5.0", "files": [
            {"path": "قوالب جاهزة/example.docx", "group": "ready_documents", "availability": "bundled"},
            {"path": "original.ai", "group": "documents", "availability": "bundled"},
        ]}
        self.records = []
        for record in self.catalog["files"]:
            data = ("content: " + record["path"]).encode("utf-8")
            record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            self.write("assets/identity/" + record["path"], data)
        self.write("SKILL.md", b"skill instructions\n")
        self.write("assets/catalog.json", BUILD.encoded_json(self.catalog))
        self.manifest = {"version": "1.5.0", "files": self.records}
        self.save_manifest()

    def write(self, relative, data):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.records.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "distribution": "repository"})

    def save_manifest(self):
        (self.root / "delivery-manifest.json").write_bytes(BUILD.encoded_json(self.manifest))

    def test_deterministic_zip_preserves_selected_bytes_and_pins_optional_sources(self):
        first, second = self.base / "first.zip", self.base / "second.zip"
        BUILD.build(self.root, first)
        BUILD.build(self.root, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            self.assertNotIn("identity-design/assets/identity/original.ai", names)
            selected = "assets/identity/قوالب جاهزة/example.docx"
            self.assertEqual(archive.read("identity-design/" + selected), (self.root / selected).read_bytes())
            catalog = json.loads(archive.read("identity-design/assets/catalog.json"))
            optional = next(r for r in catalog["files"] if r["path"] == "original.ai")
            self.assertEqual(optional["availability"], "repository_download")
            self.assertEqual(optional["source_url"], "https://raw.githubusercontent.com/Aziz93x/identity-design/v1.5.0/assets/identity/original.ai")
            manifest = json.loads(archive.read("identity-design/delivery-manifest.json"))
            for record in manifest["files"]:
                contents = archive.read("identity-design/" + record["path"])
                self.assertEqual(hashlib.sha256(contents).hexdigest(), record["sha256"])

    def test_source_tamper_refuses_build(self):
        (self.root / "SKILL.md").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "differs"):
            BUILD.build(self.root, self.base / "tampered.zip")
        self.assertFalse((self.base / "tampered.zip").exists())

    def test_existing_output_and_output_inside_source_are_refused(self):
        existing = self.base / "old.zip"
        existing.write_bytes(b"preserve")
        with self.assertRaisesRegex(ValueError, "already exists"):
            BUILD.build(self.root, existing)
        self.assertEqual(existing.read_bytes(), b"preserve")
        with self.assertRaisesRegex(ValueError, "outside"):
            BUILD.build(self.root, self.root / "result.zip")

    def test_manifest_path_traversal_is_refused(self):
        self.records[2]["path"] = "../outside.txt"
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "Non-portable"):
            BUILD.build(self.root, self.base / "unsafe.zip")

    def test_output_created_by_another_process_is_preserved(self):
        target = self.base / "raced.zip"
        original_open = Path.open

        def racing_open(path, mode="r", *args, **kwargs):
            if path == target and mode == "xb":
                with original_open(path, "wb") as other:
                    other.write(b"other process output")
                raise FileExistsError(str(path))
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", racing_open):
            with self.assertRaises(FileExistsError):
                BUILD.build(self.root, target)
        self.assertEqual(target.read_bytes(), b"other process output")

    def test_missing_selected_asset_record_is_refused(self):
        self.records.pop(0)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "missing from the manifest"):
            BUILD.build(self.root, self.base / "missing.zip")

    def test_guide_is_optional_and_retains_original_download_record(self):
        guide = {"path": "assets/identity/guide.pdf", "bytes": 100, "sha256": "a" * 64, "distribution": "existing_release_download"}
        self.records.append(guide)
        self.save_manifest()
        output = self.base / "optional.zip"
        BUILD.build(self.root, output)
        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read("identity-design/delivery-manifest.json"))
            self.assertIn(guide, manifest["files"])
            self.assertNotIn("identity-design/" + guide["path"], archive.namelist())


if __name__ == "__main__":
    unittest.main()
