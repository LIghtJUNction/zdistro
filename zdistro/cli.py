"""The same manifest drives initialization, planning, builds and evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import uuid

from . import __version__
from .backends import BACKENDS, Context, backend_for, doctor, native_build, verify_artifact
from .builders import build_language
from .model import DistroError, Manifest, copy_project, digest, expand, host_name, inside, stage_files, write_json
from .runner import Runner
from .templates import initialize, targets_from


def selected(manifest, values):
    targets = targets_from(values) or list(manifest.targets)
    for target in targets:
        backend_for(manifest, target)
    return targets


def plan(manifest, target):
    readiness = doctor(manifest, target, probe=False)
    config = manifest.targets[target]
    build = manifest.data.get("build", {})
    kind = build.get("kind", "none")
    steps = []
    if kind != "none" or target in ("crate", "wheel", "sdist", "npm", "apk", "aab", "docker") or target not in BACKENDS:
        steps.append({"action": "copy-project", "source": config.get("project", build.get("project", ".")),
                      "destination": "{build}/project", "excludes": [".git", ".venv", "node_modules", "target", "current output"]})
    if kind != "none":
        steps.append({"action": "language-build", "builder": kind,
                      "commands": build.get("commands") if kind == "command" else None,
                      "configuration": build})
    steps.append({"action": "stage", "files": manifest.file_rules(config)})
    commands = {
        "deb": [["dpkg-deb", "--root-owner-group", "--build", "{stage}", "{artifact}"]],
        "arch": [["makepkg", "--nodeps", "--nocheck", "--nosign", "--nocolor"]],
        "crate": [["cargo", "package", "--manifest-path", "{project}/Cargo.toml", "--target-dir", "{build}/cargo"]],
        "wheel": [["{python}", "-m", "build", "--wheel", "--outdir", "{out}", "{project}"]],
        "sdist": [["{python}", "-m", "build", "--sdist", "--outdir", "{out}", "{project}"]],
        "npm": [["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", "{out}", "{project}"]],
        "pkg": [["pkgbuild", "--root", "{stage}", "--identifier", manifest.package.get("identifier", "<required>"),
                 "--version", manifest.package["version"], "--install-location", "/", "{artifact}"]],
        "dmg": [["hdiutil", "create", "-srcfolder", "{stage}", "-volname", manifest.package["name"], "-format", "UDZO", "{artifact}"]],
        "msi": [["wix", "build", "-arch", config.get("architecture", "x64"), "-o", "{artifact}", "{work}/package.wxs"]],
        "msix": [["makeappx", "pack", "/d", "{stage}", "/p", "{artifact}"]],
    }
    if target in ("apk", "aab"):
        mode = config.get("mode", "aapt2" if target == "apk" else "gradle")
        commands[target] = ([["aapt2", "link", "-I", "<installed SDK android.jar>", "--manifest",
                              "{project}/" + config.get("manifest", "AndroidManifest.xml"), "-o", "{artifact}"]]
                            if mode == "aapt2" else [["<project Gradle wrapper or gradle>", "--no-daemon",
                                config.get("task", "assembleRelease" if target == "apk" else "bundleRelease")]])
    elif target == "docker":
        engine, archive = config.get("engine", "docker"), config.get("archive", "docker")
        commands[target] = ([[engine, "buildx", "build", "--output", "type=" + archive + ",dest={artifact}", "{project}"]]
                            if engine == "docker" else
                            [[engine, "build", "--tag", "<unique local build tag>", "{project}"],
                             [engine, "save", "--output", "{artifact}", "<unique local build tag>"],
                             [engine, "image", "rm", "<only this build's tag>"]])
    if target not in BACKENDS:
        recipe = manifest.data["recipes"][target]
        steps.extend([{"action": "external-build", "commands": recipe["build"], "artifact": recipe["artifact"]},
                      {"action": "external-verify", "commands": recipe["verify"]}])
    else:
        steps.extend([{"action": "package", "backend": target, "commands": commands.get(target, []),
                       "implementation": "native tool" if target in commands else "Python standard archive library"},
                      {"action": "verify", "scope": "artifact integrity/format; no installation or store submission"}])
    return {"schema": 1, "manifest_sha256": manifest.sha256, "target": target, "readiness": readiness,
            "steps": steps, "execution": "none", "placeholders": "resolved to new private build/output paths at build time"}


def build_one(manifest, target, output, timeout=600, keep_work=False):
    readiness = doctor(manifest, target, probe=True)
    if not readiness["ready"]:
        raise DistroError(f"{target} prerequisites: " + "; ".join(readiness["issues"]))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = Path(output).resolve()
    out = output / target / run_id
    out.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix=".zdistro-work-", dir=output))
    build = work / "build"
    build.mkdir()
    runner = Runner(timeout=timeout)
    base = {"schema": 1, "zdistro_version": __version__, "target": target, "host": host_name(),
            "machine": platform.machine(), "created_at": datetime.now(timezone.utc).isoformat(),
            "manifest_sha256": manifest.sha256, "package": dict(manifest.package),
            "prerequisites": readiness, "installed": False, "published": False,
            "signing": "not performed by zdistro"}
    try:
        config = manifest.targets[target]
        language = manifest.data.get("build", {})
        needs_project = language.get("kind", "none") != "none" or target in ("crate", "wheel", "sdist", "npm", "apk", "aab", "docker") or target not in BACKENDS
        project = build / "project"
        if needs_project:
            source = inside(manifest.root, config.get("project", language.get("project", ".")), "project")
            copy_project(source, project, excluded=[output])
        else:
            project.mkdir()
        epoch = int(manifest.data.get("source_date_epoch", os.environ.get("SOURCE_DATE_EPOCH", "0")))
        if not 0 <= epoch <= 4294967295:
            raise DistroError("source_date_epoch must be between 0 and 4294967295")
        ctx = Context(manifest, target, work, out, project, build, work / "stage", runner, epoch)
        build_language(ctx)
        staged = stage_files(manifest, config, build, ctx.stage, excluded=[output])
        artifact = native_build(ctx)
        verification = verify_artifact(ctx, artifact)
        receipt = {**base, "status": "verified", "artifact": artifact.relative_to(out).as_posix(),
                   "bytes": artifact.stat().st_size, "sha256": digest(artifact), "staged_files": staged,
                   "verification": verification, "commands": runner.records}
        if keep_work:
            receipt["work_directory"] = str(work)
        write_json(out / "receipt.json", receipt)
        return {"target": target, "artifact": str(artifact), "receipt": str(out / "receipt.json"),
                "sha256": receipt["sha256"], "verification": verification}
    except (DistroError, OSError, ValueError, KeyError, TypeError) as error:
        write_json(out / "failure.json", {**base, "status": "failed", "error": str(error), "commands": runner.records})
        raise DistroError(f"{target}: {error}\nFailure record: {out / 'failure.json'}") from error
    finally:
        if not keep_work:
            shutil.rmtree(work)


def verify_receipt(manifest, receipt_path, timeout=600):
    path = Path(receipt_path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or data.get("status") != "verified":
        raise DistroError("receipt does not record a successful verified build")
    if data.get("manifest_sha256") != manifest.sha256:
        raise DistroError("manifest changed since this build; use the build's original manifest")
    target = data["target"]
    backend_for(manifest, target)
    artifact = inside(path.parent, data["artifact"], "receipt artifact", False)
    if not artifact.is_file() or digest(artifact) != data["sha256"] or artifact.stat().st_size != data["bytes"]:
        raise DistroError("artifact size or SHA-256 does not match its receipt")
    with tempfile.TemporaryDirectory(prefix="zdistro-verify-") as directory:
        work = Path(directory)
        ctx = Context(manifest, target, work, path.parent, manifest.root, work / "build", work / "stage", Runner(timeout), 0)
        result = verify_artifact(ctx, artifact)
    return {"artifact": str(artifact), "sha256": data["sha256"], "verification": result}


def emit(value, as_json=True):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        for row in value:
            print(f"{row['target']}: {'ready to build' if row['ready'] else 'needs setup'} ({row['host']})")
            for item in row["issues"]:
                print(f"  - {item}")
            for item in row["warnings"]:
                print(f"  note: {item}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="One manifest for builders, native packages and verified matrices")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="generate an editable template for chosen formats")
    init.add_argument("directory", nargs="?", default=".")
    init.add_argument("--target", action="append", required=True)
    init.add_argument("--name")
    init.add_argument("--package-version", default="0.1.0")
    for name in ("doctor", "plan", "build", "verify", "matrix"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", "-m", type=Path, default=Path("zdistro.toml"))
        if name not in ("verify",):
            command.add_argument("--target", action="append")
        if name == "doctor":
            command.add_argument("--json", action="store_true")
        if name == "build":
            command.add_argument("--out", type=Path)
            command.add_argument("--available", action="store_true", help="build only ready targets, report every skipped target")
            command.add_argument("--keep-work", action="store_true")
        if name in ("build", "verify"):
            command.add_argument("--timeout", type=int, default=600)
        if name == "verify":
            command.add_argument("receipt", type=Path)
        if name == "matrix":
            command.add_argument("--format", choices=("svg", "json", "md"), default="svg")
            command.add_argument("--receipts", type=Path)
            command.add_argument("--output", "-o", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            path = initialize(args.directory, targets_from(args.target), args.name, args.package_version)
            manifest = Manifest.load(path)
            print(f"Created {path}")
            emit([doctor(manifest, target, probe=False) for target in manifest.targets], False)
            return 0
        manifest = Manifest.load(args.manifest)
        if args.command == "verify":
            emit(verify_receipt(manifest, args.receipt, args.timeout))
            return 0
        targets = selected(manifest, args.target)
        if args.command == "doctor":
            rows = [doctor(manifest, target, probe=True) for target in targets]
            emit(rows, args.json)
            return 0 if all(row["ready"] for row in rows) else 2
        if args.command == "plan":
            emit([plan(manifest, target) for target in targets])
            return 0
        if args.command == "matrix":
            from .matrix import matrix, render
            result = render(matrix(manifest, targets, args.receipts or manifest.root / "dist/zdistro"), args.format)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(result, encoding="utf-8")
                print(args.output)
            else:
                print(result, end="")
            return 0
        if args.timeout < 1:
            raise DistroError("timeout must be positive")
        results, skipped = [], []
        for target in targets:
            readiness = doctor(manifest, target, probe=False)
            if not readiness["ready"] and args.available:
                skipped.append(readiness)
                continue
            results.append(build_one(manifest, target, args.out or manifest.root / "dist/zdistro", args.timeout, args.keep_work))
        emit({"built": results, "skipped": skipped})
        return 0 if results else 2
    except (DistroError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
