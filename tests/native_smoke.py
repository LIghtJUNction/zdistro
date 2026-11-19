"""Build small real native artifacts; never install them or change host settings."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zdistro.backends import BACKENDS, doctor
from zdistro.cli import build_one, verify_receipt
from zdistro.matrix import matrix, render
from zdistro.model import Manifest, host_name, write_json
from zdistro.templates import initialize, targets_from


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", required=True)
    parser.add_argument("--root", type=Path, default=Path("dist/acceptance"))
    parser.add_argument("--android-gate", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    project = root / "project"
    if not (project / "zdistro.toml").exists():
        initialize(project, list(BACKENDS), name="zdistro-example")
    manifest = Manifest.load(project / "zdistro.toml")
    targets = targets_from([args.targets])
    if "msix" in targets:
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            raise RuntimeError("native MSIX smoke needs the Windows .NET Framework C# compiler")
        source = project / "Hello.cs"
        source.write_text('class Program { static void Main() { System.Console.WriteLine("zdistro native MSIX"); } }\n')
        subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(project / "payload/app.exe"), str(source)], check=True)
    out = root / "artifacts" / host_name()
    built = []
    for target in targets:
        result = build_one(manifest, target, out, timeout=600)
        # Independent second command path checks persisted receipts and artifacts.
        verify_receipt(manifest, result["receipt"])
        built.append(result)
        print(json.dumps({"passed": target, **result}, ensure_ascii=False), flush=True)
    gate = None
    if args.android_gate:
        gate = doctor(manifest, "aab", probe=False)
        if gate["ready"]:
            raise AssertionError("AAB sample lacks a real Gradle app; doctor must not claim it is ready")
        write_json(root / "android-prerequisites.json", gate)
        print(json.dumps({"android_aab_gate": gate}, ensure_ascii=False), flush=True)
    report = {"host": host_name(), "python": sys.version, "targets": built, "android_aab_gate": gate,
              "scope": "Artifact creation and integrity/native inspection. No package installation or store submission."}
    write_json(root / (host_name() + "-report.json"), report)
    data = matrix(manifest, list(manifest.targets), root / "artifacts")
    for format in ("svg", "json", "md"):
        (root / ("matrix." + format)).write_text(render(data, format), encoding="utf-8")
    print(json.dumps({"summary": str(root / (host_name() + "-report.json")), "passed": len(built)}), flush=True)


if __name__ == "__main__":
    main()
