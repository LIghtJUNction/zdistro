import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from zdistro.backends import doctor
from zdistro.model import DistroError, Manifest
from zdistro.templates import initialize
from zdistro.verify import verify_portable


class VerificationTests(unittest.TestCase):
    def test_oci_blob_corruption_is_rejected(self):
        payload = b"a real fixture layer" * 100000
        checksum = hashlib.sha256(payload).hexdigest()
        index = json.dumps({"schemaVersion": 2, "manifests": [
            {"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": "sha256:" + checksum, "size": len(payload)}]}).encode()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "image.tar"
            for value, expected in [(payload, True), (b"changed", False)]:
                with tarfile.open(artifact, "w") as archive:
                    for name, content in [("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'), ("index.json", index),
                                          ("blobs/sha256/" + checksum, value)]:
                        info = tarfile.TarInfo(name)
                        info.size = len(content)
                        archive.addfile(info, io.BytesIO(content))
                if expected:
                    self.assertEqual(verify_portable("docker", artifact, {"name": "fixture", "version": "1"})["level"], "structure")
                else:
                    with self.assertRaisesRegex(DistroError, "digest or size"):
                        verify_portable("docker", artifact, {"name": "fixture", "version": "1"})

    def test_unsafe_archive_member_is_rejected_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "bad.zip"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr("../outside", "untrusted")
            with self.assertRaises(DistroError):
                verify_portable("zip", artifact, {"name": "fixture", "version": "1"})
            self.assertFalse((Path(directory).parent / "outside").exists())

    def test_missing_android_sdk_remains_a_real_prerequisite(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Manifest.load(initialize(directory, ["apk", "aab"], name="android-fixture"))
            with patch.dict("os.environ", {"ANDROID_HOME": "", "ANDROID_SDK_ROOT": ""}):
                for target in ("apk", "aab"):
                    result = doctor(manifest, target)
                    self.assertFalse(result["ready"])
                    self.assertTrue(any("missing Android SDK" in issue for issue in result["issues"]))


if __name__ == "__main__":
    unittest.main()
