from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from zdistro.backends import doctor
from zdistro.cli import plan
from zdistro.model import Manifest
from zdistro.platforms import build_platform
from zdistro.templates import initialize


class ContainerPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zdistro-container-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.path = initialize(self.root, ["docker"], name="zdistro-example")
        self.manifest = Manifest.load(self.path)

    def probe(self, description):
        with patch("zdistro.backends.tool", return_value="/usr/bin/docker"), \
                patch("zdistro.backends.Runner.run", side_effect=["{}", description]) as run:
            result = doctor(self.manifest, "docker", probe=True)
        return result, run

    def test_classic_docker_driver_cannot_be_reported_ready_for_archive_export(self):
        result, run = self.probe("Name: default\nDriver: docker\nNodes:\nStatus: running\n")
        self.assertFalse(result["ready"])
        self.assertIn("cannot export Docker/OCI archives", "; ".join(result["issues"]))
        self.assertEqual(run.call_args_list[-1].args[0], ["docker", "buildx", "inspect"])
        self.assertFalse(any("--bootstrap" in call.args[0] or "create" in call.args[0]
                             for call in run.call_args_list))

    def test_supported_driver_and_containerd_snapshotter_are_accepted(self):
        for description in (
                "Name: isolated\nDriver: docker-container\nStatus: inactive\n",
                "Name: default\nDriver: docker\nLabels:\n org.mobyproject.buildkit.worker.snapshotter: overlayfs\n"):
            with self.subTest(description=description):
                result, _ = self.probe(description)
                self.assertTrue(result["ready"], result["issues"])
        result, _ = self.probe("unexpected inspect output\n")
        self.assertFalse(result["ready"])
        self.assertIn("cannot determine", "; ".join(result["issues"]))

    def test_explicit_builder_is_shared_by_probe_plan_and_build(self):
        self.path.write_text(self.path.read_text().replace('[targets.docker]',
                             '[targets.docker]\nbuilder = "zdistro-isolated"'))
        self.manifest = Manifest.load(self.path)
        result, run = self.probe("Name: zdistro-isolated\nDriver: docker-container\n")
        self.assertTrue(result["ready"], result["issues"])
        self.assertEqual(run.call_args_list[-1].args[0], ["docker", "buildx", "inspect", "zdistro-isolated"])
        with patch("subprocess.run", side_effect=AssertionError("plan executed a command")):
            planned = plan(self.manifest, "docker")
        command = next(step["commands"][0] for step in planned["steps"] if step["action"] == "package")
        self.assertEqual(command[3:5], ["--builder", "zdistro-isolated"])
        context = SimpleNamespace(target="docker", config=self.manifest.targets["docker"],
                                  package=self.manifest.package, project=self.root / "container", run=Mock())
        build_platform(context, self.root / "example.tar")
        actual = context.run.call_args.args[0]
        self.assertEqual(actual[3:5], ["--builder", "zdistro-isolated"])
        self.assertEqual(context.run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
