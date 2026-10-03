"""Offline download tests with synthetic bytes and an in-memory response."""

from contextlib import redirect_stdout
import hashlib
from http.client import HTTPResponse
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

__author__ = "Abdulaziz Almalki"

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_assets.py"
SPEC = importlib.util.spec_from_file_location("identity_prepare_assets", SCRIPT)
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class PrepareAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="identity-download-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "assets" / "identity"
        self.manifest = self.base / "assets" / "downloads.json"
        self.manifest.parent.mkdir(parents=True)
        self.data = b"%PDF-1.7\nSynthetic test data, not the actual guide.\n"
        self.record = self.make_record(self.data)
        self.write_manifest(self.record)
        # No test is allowed to reach a real server, even accidentally.
        self.network = Mock(side_effect=AssertionError("Real network access is forbidden in these tests"))
        network_patch = patch.object(PREPARE, "urlopen", self.network)
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def make_record(self, data):
        return {"id": "guide", "path": PREPARE.GUIDE_NAME, "url": PREPARE.GUIDE_URL,
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def write_manifest(self, record):
        self.manifest.write_text(json.dumps({"downloads": [record]}, ensure_ascii=False), encoding="utf-8")

    def run_prepare(self, opener=None):
        return PREPARE.prepare_assets(str(self.root), self.manifest, opener)

    def assert_no_partial_output(self):
        self.assertFalse((self.root / PREPARE.GUIDE_NAME).exists())
        self.assertEqual(list(self.root.glob(".identity-guide-*.part")), [])

    def test_chunked_download_verifies_and_publishes_in_destination(self):
        data = b"%PDF-1.7\n" + b"x" * (PREPARE.CHUNK_SIZE + 123)
        self.write_manifest(self.make_record(data))
        sizes = []

        class BoundedResponse(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= PREPARE.CHUNK_SIZE:
                    raise AssertionError("Download read must be bounded")
                sizes.append(size)
                return super().read(size)

        def open_response(request, timeout):
            self.assertEqual(request.full_url, PREPARE.GUIDE_URL)
            self.assertGreater(timeout, 0)
            self.assertFalse((self.root / PREPARE.GUIDE_NAME).exists())
            self.assertEqual(len(list(self.root.glob(".identity-guide-*.part"))), 1)
            return BoundedResponse(data)

        report = self.run_prepare(open_response)
        self.assertEqual(report["action"], "downloaded")
        self.assertEqual((self.root / PREPARE.GUIDE_NAME).read_bytes(), data)
        self.assertGreater(len(sizes), 1)
        self.assertEqual(list(self.root.glob(".identity-guide-*.part")), [])

    def test_short_stream_reads_are_supported(self):
        class ShortResponse(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 2))

        report = self.run_prepare(lambda request, timeout: ShortResponse(self.data))
        self.assertEqual(report["action"], "downloaded")
        self.assertEqual((self.root / PREPARE.GUIDE_NAME).read_bytes(), self.data)

    def test_matching_existing_file_skips_network(self):
        self.root.mkdir()
        (self.root / PREPARE.GUIDE_NAME).write_bytes(self.data)
        report = self.run_prepare()
        self.assertEqual(report["action"], "already_present")
        self.network.assert_not_called()

    def test_existing_wrong_hash_is_untouched(self):
        self.root.mkdir()
        existing = b"z" * len(self.data)
        target = self.root / PREPARE.GUIDE_NAME
        target.write_bytes(existing)
        with self.assertRaisesRegex(ValueError, "left unchanged"):
            self.run_prepare()
        self.assertEqual(target.read_bytes(), existing)
        self.network.assert_not_called()
        self.assertEqual(list(self.root.glob(".identity-guide-*.part")), [])

    def test_wrong_download_hash_leaves_no_final_or_temporary_file(self):
        self.record["sha256"] = "0" * 64
        self.write_manifest(self.record)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.run_prepare(lambda request, timeout: io.BytesIO(self.data))
        self.assert_no_partial_output()

    def test_short_and_oversized_downloads_fail(self):
        for payload in (self.data[:-1], self.data + b"extra"):
            with self.subTest(length=len(payload)):
                with self.assertRaises(ValueError):
                    self.run_prepare(lambda request, timeout: io.BytesIO(payload))
                self.assert_no_partial_output()

    def test_non_pdf_is_rejected_even_with_a_matching_manifest_hash(self):
        invalid = b"This is not a PDF or any real asset."
        self.write_manifest(self.make_record(invalid))
        with self.assertRaisesRegex(ValueError, "PDF signature"):
            self.run_prepare(lambda request, timeout: io.BytesIO(invalid))
        self.assert_no_partial_output()

    def test_interrupted_download_removes_temporary_file(self):
        class InterruptedResponse(io.BytesIO):
            def read(self, size=-1):
                if self.tell() > 0:
                    raise OSError("Synthetic interrupted transfer")
                return super().read(min(size, 8))

        with self.assertRaisesRegex(OSError, "interrupted"):
            self.run_prepare(lambda request, timeout: InterruptedResponse(self.data))
        self.assert_no_partial_output()

    def test_truncated_http_chunk_returns_json_failure_and_cleans_up(self):
        # Exercise the real HTTP chunk decoder, not an exception-only mock.
        wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"100\r\n%PDF-1.7\ntruncated")

        class FakeSocket:
            def makefile(self, mode):
                return io.BytesIO(wire)

        response = HTTPResponse(FakeSocket())
        response.begin()
        output = io.StringIO()
        self.network.side_effect = None
        self.network.return_value = response
        with patch.object(PREPARE, "MANIFEST", self.manifest), redirect_stdout(output):
            result = PREPARE.main(["--root", str(self.root)])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "fail")
        self.assertIn("IncompleteRead", report["error"])
        self.network.assert_called_once()
        self.assert_no_partial_output()

    def test_traversal_absolute_paths_and_other_assets_are_rejected(self):
        for relative in ("../outside.pdf", "a/../../outside.pdf", "..\\outside.pdf", "/outside.pdf", "C:/outside.pdf", "C:outside.pdf", "\\\\server\\share\\outside.pdf", "font.ttf"):
            with self.subTest(path=relative):
                record = dict(self.record, path=relative)
                self.write_manifest(record)
                with self.assertRaises(ValueError):
                    self.run_prepare()
                self.network.assert_not_called()
                self.assertFalse(self.root.exists())

    def test_resolved_destination_must_remain_inside_root(self):
        root = self.root.resolve()
        with self.assertRaisesRegex(ValueError, "outside"):
            PREPARE.target_path(root, "../outside.pdf")

    def test_only_approved_guide_record_is_accepted(self):
        cases = [("id", "font"), ("url", "https://example.com/guide.pdf"), ("bytes", True), ("bytes", 0), ("bytes", "10"), ("sha256", "bad")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.write_manifest(dict(self.record, **{key: value}))
                with self.assertRaises(ValueError):
                    self.run_prepare()
                self.network.assert_not_called()

    def test_manifest_requires_exactly_one_record(self):
        for value in ({"downloads": []}, {"downloads": [self.record, self.record]}, [self.record]):
            with self.subTest(value=value):
                self.manifest.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.run_prepare()
                self.network.assert_not_called()

    def test_a_file_created_during_download_is_not_overwritten(self):
        competing = b"Another process wrote this file."

        def open_response(request, timeout):
            (self.root / PREPARE.GUIDE_NAME).write_bytes(competing)
            return io.BytesIO(self.data)

        with self.assertRaisesRegex(ValueError, "left unchanged"):
            self.run_prepare(open_response)
        self.assertEqual((self.root / PREPARE.GUIDE_NAME).read_bytes(), competing)
        self.assertEqual(list(self.root.glob(".identity-guide-*.part")), [])

    def test_native_publication_refuses_existing_file(self):
        source = self.base / "staging"
        target = self.base / "target"
        source.write_bytes(b"new")
        target.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            PREPARE.publish_without_overwrite(source, target)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(source.read_bytes(), b"new")

    def test_publication_dispatches_to_platform_appropriate_primitive(self):
        for windows in (True, False):
            with self.subTest(windows=windows):
                source, target = self.base / "staging", self.base / "target"
                with patch.object(PREPARE, "IS_WINDOWS", windows), \
                        patch.object(PREPARE.os, "rename") as rename, \
                        patch.object(PREPARE.os, "link") as link:
                    PREPARE.publish_without_overwrite(source, target)
                    selected, unused = (rename, link) if windows else (link, rename)
                    selected.assert_called_once_with(source, target)
                    unused.assert_not_called()

    def test_default_destination_is_bundle_and_empty_override_fails(self):
        with patch.object(PREPARE, "BUNDLED_ROOT", self.root), patch.object(PREPARE, "MANIFEST", self.manifest):
            report = PREPARE.prepare_assets(opener=lambda request, timeout: io.BytesIO(self.data), environ={})
            self.assertEqual(report["root"], str(self.root.resolve()))
            self.assertEqual(report["root_source"], "bundled_assets")
            with self.assertRaisesRegex(ValueError, "empty"):
                PREPARE.prepare_assets("", opener=self.network, environ={})
        self.network.assert_not_called()

    def test_environment_selects_destination_instead_of_bundle(self):
        unused = self.base / "unused-bundle"
        with patch.object(PREPARE, "BUNDLED_ROOT", unused):
            report = PREPARE.prepare_assets(manifest_path=self.manifest, opener=lambda request, timeout: io.BytesIO(self.data), environ={PREPARE.ENVIRONMENT_VARIABLE: str(self.root)})
        self.assertEqual(report["root"], str(self.root.resolve()))
        self.assertEqual(report["root_source"], PREPARE.ENVIRONMENT_VARIABLE)
        self.assertEqual((self.root / PREPARE.GUIDE_NAME).read_bytes(), self.data)
        self.assertFalse(unused.exists())

    def test_explicit_root_wins_over_environment(self):
        unused = self.base / "unused-environment-root"
        report = PREPARE.prepare_assets(str(self.root), self.manifest, lambda request, timeout: io.BytesIO(self.data), environ={PREPARE.ENVIRONMENT_VARIABLE: str(unused)})
        self.assertEqual(report["root_source"], "--root")
        self.assertEqual((self.root / PREPARE.GUIDE_NAME).read_bytes(), self.data)
        self.assertFalse(unused.exists())

    def test_empty_environment_fails_without_using_bundle(self):
        with patch.object(PREPARE, "BUNDLED_ROOT", self.root):
            for value in ("", " "):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "IDENTITY_DESIGN_ROOT is empty"):
                        PREPARE.prepare_assets(manifest_path=self.manifest, environ={PREPARE.ENVIRONMENT_VARIABLE: value})
        self.network.assert_not_called()
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
