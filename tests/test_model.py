import json
from pathlib import Path
import tempfile
import unittest

from zdistro.model import DistroError, Manifest, expand, relative_path, stage_files


BASE = '''schema = 1
[package]
name = "zdistro-test"
version = "0.1.0"
description = "A package fixture"
[[files]]
source = "payload"
destination = "usr/share/test"
[targets.zip]
'''


class ManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "zdistro.toml").write_text(BASE)
        (self.root / "payload").mkdir()
        (self.root / "payload/hello.txt").write_text("hello 世界\n", encoding="utf-8")

    def test_staging_keeps_names_content_and_permissions(self):
        manifest = Manifest.load(self.root / "zdistro.toml")
        stage = self.root / "stage"
        rows = stage_files(manifest, {}, self.root / "build", stage)
        self.assertEqual([row["path"] for row in rows], ["usr/share/test/hello.txt"])
        self.assertEqual((stage / rows[0]["path"]).read_text(encoding="utf-8"), "hello 世界\n")
        self.assertEqual(len(rows[0]["sha256"]), 64)

    def test_path_traversal_and_windows_absolute_paths_are_rejected(self):
        for value in ["../outside", "/etc/passwd", "C:/data", "a/../../x", ".git/config", "x\\y"]:
            with self.subTest(value=value), self.assertRaises(DistroError):
                relative_path(value)

    def test_missing_source_and_colliding_files_fail(self):
        manifest = Manifest.load(self.root / "zdistro.toml")
        with self.assertRaisesRegex(DistroError, "missing"):
            stage_files(manifest, {"files": [{"source": "absent", "destination": "x"}]}, self.root, self.root / "bad")
        rules = [{"source": "payload/hello.txt", "destination": "X"},
                 {"source": "payload/hello.txt", "destination": "x"}]
        with self.assertRaisesRegex(DistroError, "colliding"):
            stage_files(manifest, {"files": rules}, self.root, self.root / "collision")

    def test_placeholders_are_literal_and_do_not_expose_object_attributes(self):
        self.assertEqual(expand("{stage}/$(touch x);`id`", {"stage": "/tmp/a b"}), "/tmp/a b/$(touch x);`id`")
        for value in ["{stage.__class__}", "{secret}", "{stage!r}"]:
            with self.assertRaises(DistroError):
                expand(value, {"stage": "/tmp"})

    def test_custom_recipe_requires_real_verification(self):
        path = self.root / "zdistro.toml"
        path.write_text(BASE + '\n[recipes.rpm]\nbuild = [["rpmbuild"]]\nartifact = "out.rpm"\nverify = []\n')
        with self.assertRaisesRegex(DistroError, "both build and verify"):
            Manifest.load(path)


if __name__ == "__main__":
    unittest.main()
