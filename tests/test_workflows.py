from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from zdistro.cli import build_one, main, plan, verify_receipt
from zdistro.matrix import matrix, render
from zdistro.model import DistroError, Manifest, digest
from zdistro.templates import initialize, targets_from


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zdistro-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project with spaces"
        self.path = initialize(self.root, ["zip", "tar-gz", "extension", "xpi"], name="zdistro-example")
        self.manifest = Manifest.load(self.path)
        self.output = self.root / "dist/zdistro"

    def test_all_portable_outputs_have_real_receipts_and_reverify(self):
        for target in self.manifest.targets:
            with self.subTest(target=target):
                built = build_one(self.manifest, target, self.output)
                receipt = json.loads(Path(built["receipt"]).read_text())
                self.assertGreater(receipt["bytes"], 0)
                self.assertEqual(receipt["status"], "verified")
                self.assertFalse(receipt["installed"])
                self.assertFalse(receipt["published"])
                self.assertEqual(verify_receipt(self.manifest, built["receipt"])["sha256"], built["sha256"])

    def test_reproducible_portable_archive_and_tamper_detection(self):
        first = build_one(self.manifest, "zip", self.output)
        second = build_one(self.manifest, "zip", self.output)
        self.assertEqual(first["sha256"], second["sha256"])
        with Path(first["artifact"]).open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(DistroError, "SHA-256"):
            verify_receipt(self.manifest, first["receipt"])

    def test_extension_missing_resource_does_not_generate_success_receipt(self):
        path = self.root / "extension/manifest.json"
        data = json.loads(path.read_text())
        data["background"] = {"service_worker": "missing.js"}
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(DistroError, "resource is missing"):
            build_one(self.manifest, "extension", self.output)
        self.assertEqual(list(self.output.rglob("receipt.json")), [])
        self.assertEqual(len(list(self.output.rglob("failure.json"))), 1)

    def test_plan_does_not_run_any_builder(self):
        with patch("subprocess.run", side_effect=AssertionError("plan ran a tool")):
            result = plan(self.manifest, "zip")
        self.assertEqual(result["execution"], "none")
        self.assertFalse(self.output.exists())

    def test_matrix_only_marks_actual_integrity_checked_artifacts(self):
        empty = matrix(self.manifest, list(self.manifest.targets), self.output)
        self.assertFalse(any(cell["status"].startswith("verified") for row in empty["rows"] for cell in row["platforms"].values()))
        built = build_one(self.manifest, "zip", self.output)
        observed = matrix(self.manifest, list(self.manifest.targets), self.output)
        verified = [cell for row in observed["rows"] for cell in row["platforms"].values() if cell["status"].startswith("verified")]
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["status"], "verified-structure")
        ET.fromstring(render(observed, "svg"))
        self.assertIn("Integrity verified", render(observed, "md"))
        Path(built["artifact"]).unlink()
        missing = matrix(self.manifest, ["zip"], self.output)
        self.assertTrue(any(cell["status"] == "receipt-only" for cell in missing["rows"][0]["platforms"].values()))

    def test_manifest_change_invalidates_old_receipt(self):
        built = build_one(self.manifest, "zip", self.output)
        self.path.write_text(self.path.read_text() + "\n# changed configuration\n")
        with self.assertRaisesRegex(DistroError, "manifest changed"):
            verify_receipt(Manifest.load(self.path), built["receipt"])

    def test_init_aliases_and_no_overwrite(self):
        self.assertEqual(targets_from(["ubuntu,安卓", "浏览器插件", "rust,python,docker"]),
                         ["deb", "apk", "extension", "crate", "wheel", "docker"])
        before = self.path.read_bytes()
        with self.assertRaises(DistroError):
            initialize(self.root, ["msi"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_all_builtin_templates_load_and_plan_without_tools(self):
        from zdistro.backends import BACKENDS
        directory = self.root.parent / "all"
        manifest = Manifest.load(initialize(directory, list(BACKENDS), name="zdistro-all"))
        for target in manifest.targets:
            with self.subTest(target=target), patch("subprocess.run", side_effect=AssertionError("plan executed a command")):
                self.assertEqual(plan(manifest, target)["execution"], "none")

    def test_external_recipe_uses_real_artifact_and_verifier(self):
        path = self.root / "zdistro.toml"
        source = path.read_text() + '''
[targets.custom]
files = []
[recipes.custom]
artifact = "result.bin"
build = [["{python}", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'CUSTOM')", "{artifact}"]]
verify = [["{python}", "-c", "from pathlib import Path; import sys; assert Path(sys.argv[1]).read_bytes() == b'CUSTOM'", "{artifact}"]]
'''
        path.write_text(source)
        manifest = Manifest.load(path)
        result = build_one(manifest, "custom", self.output)
        self.assertEqual(result["verification"]["level"], "recipe")
        self.assertEqual(Path(result["artifact"]).read_bytes(), b"CUSTOM")


if __name__ == "__main__":
    unittest.main()
