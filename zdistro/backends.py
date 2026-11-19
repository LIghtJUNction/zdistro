"""Native adapters share staging and receipts, never substitute fake formats."""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tarfile
import time
import zipfile

from .model import DistroError, expand, host_name, inside, relative_path, string_list, text
from .runner import Runner, tool
from .verify import verify_portable


ALL_HOSTS = ("linux", "macos", "windows")


@dataclass(frozen=True)
class Backend:
    name: str
    hosts: tuple
    tools: tuple
    suffix: str
    description: str


BACKENDS = {item.name: item for item in [
    Backend("deb", ("linux", "macos"), ("dpkg-deb",), ".deb", "Debian / Ubuntu binary package"),
    Backend("arch", ("linux",), ("makepkg", "fakeroot", "pacman", "bsdtar"), ".pkg.tar.gz", "Arch / ALPM binary package"),
    Backend("zip", ALL_HOSTS, (), ".zip", "portable ZIP archive"),
    Backend("tar-gz", ALL_HOSTS, (), ".tar.gz", "portable gzip tar archive"),
    Backend("crate", ALL_HOSTS, ("cargo",), ".crate", "Cargo source package with build verification"),
    Backend("wheel", ALL_HOSTS, (), ".whl", "Python wheel through PEP 517"),
    Backend("sdist", ALL_HOSTS, (), ".tar.gz", "Python source distribution through PEP 517"),
    Backend("npm", ALL_HOSTS, ("npm",), ".tgz", "npm package tarball"),
    Backend("extension", ALL_HOSTS, (), ".zip", "browser extension upload archive"),
    Backend("xpi", ALL_HOSTS, (), ".xpi", "unsigned Firefox extension archive"),
    Backend("pkg", ("macos",), ("pkgbuild", "pkgutil"), ".pkg", "macOS Installer component package"),
    Backend("dmg", ("macos",), ("hdiutil",), ".dmg", "macOS read-only disk image"),
    Backend("msi", ("windows",), ("wix",), ".msi", "WiX Windows Installer package"),
    Backend("msix", ("windows",), ("makeappx",), ".msix", "Windows SDK app package"),
    Backend("apk", ALL_HOSTS, (), ".apk", "Android APK through AAPT2 or Gradle"),
    Backend("aab", ALL_HOSTS, ("java",), ".aab", "Android App Bundle through AAPT2/Gradle and bundletool"),
    Backend("docker", ALL_HOSTS, (), ".tar", "Docker or OCI image archive"),
]}


def sdk_root():
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(key)
        if value and Path(value).is_dir():
            return Path(value).resolve()
    return None


def android_tool(name):
    found = tool(name)
    if found:
        return found
    root = sdk_root()
    suffix = ".exe" if host_name() == "windows" else ""
    if root:
        paths = sorted(root.glob(f"build-tools/*/{name}{suffix}"), reverse=True)
        if paths:
            return str(paths[0])
    return None


def windows_tool(name):
    found = tool(name)
    if found:
        return found
    if host_name() == "windows":
        root = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits/10/bin"
        paths = sorted(root.glob(f"*/x64/{name}.exe"), reverse=True)
        if paths:
            return str(paths[0])
    return None


def android_platform(config):
    root = sdk_root()
    if not root:
        return None
    if config.get("platform"):
        value = root / "platforms" / str(relative_path(config["platform"], "Android platform")) / "android.jar"
        return value if value.is_file() else None
    paths = list(root.glob("platforms/android-*/android.jar"))
    return max(paths, key=lambda item: int(re.search(r"android-(\d+)", str(item)).group(1))
               if re.search(r"android-(\d+)", str(item)) else 0) if paths else None


def bundletool_command(config):
    jar = config.get("bundletool_jar") or os.environ.get("BUNDLETOOL_JAR")
    if jar and Path(jar).is_file():
        return ["java", "-jar", str(Path(jar).resolve())]
    executable = tool("bundletool")
    return [executable] if executable else None


def backend_for(manifest, target):
    if target not in manifest.targets:
        raise DistroError(f"target is not declared in manifest: {target}")
    if target in BACKENDS:
        if target in manifest.data.get("recipes", {}):
            raise DistroError(f"custom recipe cannot silently replace builtin backend: {target}")
        return BACKENDS[target]
    recipe = manifest.data.get("recipes", {}).get(target)
    if recipe is None:
        raise DistroError(f"unknown target {target}; add [recipes.{target}] with build and verify argv")
    return Backend(target, tuple(recipe.get("hosts", ALL_HOSTS)), tuple(recipe.get("tools", [])),
                   Path(recipe["artifact"]).suffix, "explicit external tool recipe")


def doctor(manifest, target, probe=False):
    backend = backend_for(manifest, target)
    config = manifest.targets[target]
    issues, tools, warnings = [], {}, []
    if host_name() not in backend.hosts:
        issues.append(f"native backend needs {', '.join(backend.hosts)}; current host is {host_name()}")
    requirements = list(backend.tools)
    if target == "docker":
        requirements.append(config.get("engine", "docker"))
    build = manifest.data.get("build", {})
    if build.get("kind") in ("cargo", "npm"):
        requirements.append(build["kind"])
    if build.get("kind") == "command":
        requirements.extend(build.get("tools", []))
    for requirement in requirements:
        found = windows_tool(requirement) if requirement == "makeappx" else tool(requirement)
        tools[requirement] = found
        if not found:
            issues.append(f"missing tool: {requirement}")
    if target in ("wheel", "sdist") or build.get("kind") == "python":
        found = importlib.util.find_spec("build") is not None
        tools["python -m build"] = sys.executable if found else None
        if not found:
            issues.append("missing PEP 517 frontend: install build in the Python environment running zdistro")
    project = inside(manifest.root, config.get("project", build.get("project", ".")), "project")
    if target in ("crate", "wheel", "sdist", "npm"):
        expected = {"crate": "Cargo.toml", "wheel": "pyproject.toml", "sdist": "pyproject.toml", "npm": "package.json"}[target]
        if not (project / expected).is_file():
            issues.append(f"missing language project: {expected} in {project}")
    if target in ("deb", "arch") and not manifest.package.get("maintainer"):
        issues.append("missing package.maintainer")
    if target == "deb" and not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", manifest.package["name"]):
        issues.append("Debian package names need at least two lowercase letters/digits/+.-")
    if target == "arch":
        if "-" in manifest.package["version"]:
            issues.append("Arch pkgver cannot contain '-'; put release number in targets.arch.release")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            issues.append("makepkg requires an unprivileged user; zdistro never changes users automatically")
    if target in ("pkg", "msix") and not manifest.package.get("identifier"):
        issues.append("missing package.identifier")
    if target == "msix" and not config.get("executable"):
        issues.append("missing targets.msix.executable: staged Windows executable path")
    if target == "msix" and not re.fullmatch(r"[-.A-Za-z0-9]+", config.get("identity", manifest.package.get("identifier", ""))):
        issues.append("MSIX identity uses letters/digits/dot/hyphen; set targets.msix.identity for a platform-specific identity")
    if target in ("apk", "aab"):
        application_id = config.get("application_id", manifest.package.get("identifier", ""))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", application_id):
            issues.append(f"missing or invalid targets.{target}.application_id (or explicit package.identifier)")
        if not sdk_root():
            issues.append("missing Android SDK: set ANDROID_HOME or ANDROID_SDK_ROOT to an existing SDK")
        mode = config.get("mode", "aapt2" if target == "apk" else "gradle")
        if mode not in ("aapt2", "gradle"):
            issues.append("Android mode must be aapt2 or gradle")
        if mode == "aapt2":
            tools["aapt2"] = android_tool("aapt2")
            if not tools["aapt2"]:
                issues.append("missing Android tool: aapt2")
            if not android_platform(config):
                issues.append("missing installed Android SDK platform android.jar")
            if not inside(project, config.get("manifest", "AndroidManifest.xml")).is_file():
                issues.append("missing AndroidManifest.xml for AAPT2 link")
        else:
            wrapper = project / ("gradlew.bat" if host_name() == "windows" else "gradlew")
            if not any((project / path).is_file() for path in ("build.gradle", "build.gradle.kts")):
                issues.append("missing real Gradle Android project: build.gradle or build.gradle.kts")
            if not wrapper.is_file() and not tool("gradle"):
                issues.append("missing Gradle wrapper or gradle executable")
            if wrapper.is_file() and not (project / "gradle/wrapper/gradle-wrapper.jar").is_file():
                issues.append("Gradle wrapper is incomplete: gradle-wrapper.jar is missing")
            if not config.get("artifact"):
                issues.append(f"missing targets.{target}.artifact: project-relative Gradle output")
            if not tool("java"):
                issues.append("missing tool: java")
            if target == "apk" and not android_tool("aapt2"):
                issues.append("missing APK native verifier: aapt2")
        if target == "aab" and not bundletool_command(config):
            issues.append("missing bundletool: provide targets.aab.bundletool_jar or BUNDLETOOL_JAR")
        warnings.append("APK/AAB output is not a store submission or signing guarantee")
    for rule in manifest.file_rules(config):
        if rule["source"].startswith("{build}/"):
            if build.get("kind", "none") == "none":
                issues.append("files.source uses {build} but build.kind is none")
        elif not inside(manifest.root, rule["source"], "source file").exists():
            issues.append(f"missing source: {rule['source']}")
    if target == "docker":
        dockerfile = inside(project, config.get("dockerfile", "Dockerfile"), "Dockerfile")
        if not dockerfile.is_file():
            issues.append("missing Dockerfile in the declared project")
        engine = config.get("engine", "docker")
        if engine not in ("docker", "podman"):
            issues.append("docker engine must be docker or podman")
        if config.get("archive", "docker") not in ("docker", "oci"):
            issues.append("container archive must be docker or oci")
        if probe and tool(engine):
            try:
                Runner(timeout=15).run([engine, "info", "--format", "json"], manifest.root)
                if engine == "docker":
                    Runner(timeout=15).run([engine, "buildx", "version"], manifest.root)
            except DistroError as error:
                issues.append(f"container engine is unavailable: {error}")
    if target in ("pkg", "dmg", "msi", "msix", "xpi"):
        warnings.append("unsigned build; installation, signing and store approval are separate checks")
    return {"target": target, "host": host_name(), "ready": not issues, "issues": issues,
            "tools": tools, "warnings": warnings, "hosts": list(backend.hosts), "description": backend.description}


@dataclass
class Context:
    manifest: object
    target: str
    work: Path
    out: Path
    project: Path
    build: Path
    stage: Path
    runner: Runner
    epoch: int

    @property
    def config(self):
        return self.manifest.targets[self.target]

    @property
    def package(self):
        return self.manifest.package

    @property
    def variables(self):
        return {"project": self.project, "stage": self.stage, "build": self.build, "work": self.work,
                "out": self.out, "name": self.package["name"], "version": self.package["version"],
                "python": sys.executable}

    @property
    def env(self):
        return {"SOURCE_DATE_EPOCH": str(self.epoch)}

    def run(self, argv, cwd=None, env=None):
        return self.runner.run(argv, cwd or self.work, {**self.env, **(env or {})})


def portable_zip(stage, artifact, epoch):
    date = time.gmtime(max(315532800, min(epoch, 4354819198)))[:6]
    with zipfile.ZipFile(artifact, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                entry = zipfile.ZipInfo(file.relative_to(stage).as_posix(), date_time=date)
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = (file.stat().st_mode & 0xFFFF) << 16
                with file.open("rb") as source, archive.open(entry, "w") as output:
                    shutil.copyfileobj(source, output)


def portable_tar(stage, artifact, epoch):
    with artifact.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for file in sorted(stage.rglob("*")):
                entry = archive.gettarinfo(str(file), arcname=file.relative_to(stage).as_posix())
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = "root"
                entry.mtime = epoch
                if file.is_file():
                    with file.open("rb") as stream:
                        archive.addfile(entry, stream)
                else:
                    archive.addfile(entry)


def native_build(ctx):
    target, config, package = ctx.target, ctx.config, ctx.package
    backend = backend_for(ctx.manifest, target)
    artifact = ctx.out / f"{package['name']}-{package['version']}{backend.suffix}"
    if target in ("zip", "extension", "xpi"):
        portable_zip(ctx.stage, artifact, ctx.epoch)
    elif target == "tar-gz":
        portable_tar(ctx.stage, artifact, ctx.epoch)
    elif target == "deb":
        metadata = ctx.stage / "DEBIAN"
        if metadata.exists():
            raise DistroError("DEBIAN is reserved for generated package metadata")
        metadata.mkdir(mode=0o755)
        metadata.chmod(0o755)  # Preserve native metadata permissions under a restrictive umask.
        fields = {"Package": package["name"], "Version": package["version"],
                  "Architecture": text(config.get("architecture"), "Debian architecture", "all"),
                  "Maintainer": package["maintainer"], "Description": package["description"],
                  "Installed-Size": str((sum(file.stat().st_size for file in ctx.stage.rglob("*") if file.is_file()) + 1023) // 1024)}
        if config.get("depends"):
            fields["Depends"] = ", ".join(string_list(config["depends"], "Debian depends"))
        if package.get("homepage"):
            fields["Homepage"] = package["homepage"]
        (metadata / "control").write_text("".join(f"{key}: {text(value, key)}\n" for key, value in fields.items()), encoding="utf-8")
        (metadata / "control").chmod(0o644)
        ctx.run(["dpkg-deb", "--root-owner-group", "--build", ctx.stage, artifact])
    elif target == "arch":
        if any((ctx.stage / name).exists() for name in (".PKGINFO", ".BUILDINFO", ".MTREE", ".INSTALL")):
            raise DistroError("ALPM metadata and install scripts require the native backend or an explicit recipe")
        recipe = ctx.work / "arch"
        recipe.mkdir()
        arch = text(config.get("architecture"), "Arch architecture", "any")
        release = text(str(config.get("release", "1")), "Arch release")
        q = shlex.quote
        lines = [f"pkgname={q(package['name'])}", f"pkgver={q(package['version'])}", f"pkgrel={q(release)}",
                 f"pkgdesc={q(package['description'])}", f"arch=({q(arch)})",
                 f"url={q(package.get('homepage', ''))}", f"license=({q(package.get('license', 'unknown'))})",
                 "options=('!strip' '!debug')"]
        if config.get("depends"):
            lines.append("depends=(" + " ".join(q(item) for item in string_list(config["depends"], "Arch depends")) + ")")
        lines.extend(["package() {", f"  cp -a -- {q(str(ctx.stage))}/. \"$pkgdir/\"", "}", ""])
        (recipe / "PKGBUILD").write_text("\n".join(lines), encoding="utf-8")
        ctx.run(["makepkg", "--nodeps", "--nocheck", "--nosign", "--nocolor"], recipe,
                {"PKGDEST": str(ctx.out), "PKGEXT": ".pkg.tar.gz", "PACKAGER": package["maintainer"]})
        artifacts = list(ctx.out.glob("*.pkg.tar.gz"))
        if len(artifacts) != 1:
            raise DistroError("makepkg did not produce exactly one package")
        artifact = artifacts[0]
    elif target in ("crate", "wheel", "sdist", "npm"):
        if target == "crate":
            ctx.run(["cargo", "package", "--manifest-path", ctx.project / "Cargo.toml", "--target-dir", ctx.build / "cargo"], ctx.project)
            choices = list((ctx.build / "cargo/package").glob("*.crate"))
        elif target in ("wheel", "sdist"):
            argv = [sys.executable, "-m", "build", "--" + target, "--outdir", str(ctx.out)]
            if config.get("isolation", True) is False:
                argv.append("--no-isolation")
            ctx.run([*argv, ctx.project], ctx.project)
            choices = list(ctx.out.glob("*.whl" if target == "wheel" else "*.tar.gz"))
        else:
            ctx.run(["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", ctx.out, ctx.project], ctx.project)
            choices = list(ctx.out.glob("*.tgz"))
        if len(choices) != 1:
            raise DistroError(f"{target} builder must produce exactly one artifact; select a single package project")
        artifact = choices[0]
        if artifact.parent != ctx.out:
            shutil.copyfile(artifact, ctx.out / artifact.name)
            artifact = ctx.out / artifact.name
    elif target in ("pkg", "dmg", "msi", "msix", "apk", "aab", "docker"):
        from .platforms import build_platform
        artifact = build_platform(ctx, artifact)
    else:
        recipe = ctx.manifest.data["recipes"][target]
        artifact = inside(ctx.out, recipe["artifact"], "recipe artifact", False)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        ctx.runner.commands(recipe["build"], {**ctx.variables, "artifact": artifact}, ctx.project, ctx.env)
    if not artifact.is_file() or artifact.is_symlink() or not artifact.stat().st_size:
        raise DistroError(f"builder did not create a nonempty regular artifact: {artifact}")
    if not artifact.resolve().is_relative_to(ctx.out.resolve()):
        raise DistroError("builder artifact escaped its output directory")
    return artifact


def verify_artifact(ctx, artifact):
    target = ctx.target
    if target == "deb":
        result = ctx.run(["dpkg-deb", "--field", artifact, "Package", "Version"])
        if f"Package: {ctx.package['name']}" not in result or f"Version: {ctx.package['version']}" not in result:
            raise DistroError("Debian package metadata differs from manifest")
        ctx.run(["dpkg-deb", "--contents", artifact])
        return {"level": "native", "checks": ["dpkg-deb metadata identity and payload listing"]}
    if target == "arch":
        database = ctx.work / "pacman-db"
        database.mkdir(exist_ok=True)
        ctx.run(["pacman", "--dbpath", database, "--query", "--info", "--file", artifact])
        names = ctx.run(["bsdtar", "-tf", artifact]).splitlines()
        if not {".PKGINFO", ".BUILDINFO", ".MTREE"} <= {name.removeprefix("./") for name in names}:
            raise DistroError("Arch package lacks mandatory ALPM metadata")
        metadata = ctx.run(["bsdtar", "-xOf", artifact, ".PKGINFO"])
        if f"pkgname = {ctx.package['name']}\n" not in metadata + "\n":
            raise DistroError("Arch package name differs from manifest")
        return {"level": "native", "checks": ["pacman package inspection", "ALPM .PKGINFO/.BUILDINFO/.MTREE presence"]}
    if target in ("pkg", "dmg", "msi", "msix", "apk", "aab"):
        from .platforms import verify_platform
        return verify_platform(ctx, artifact)
    if target not in BACKENDS:
        recipe = ctx.manifest.data["recipes"][target]
        ctx.runner.commands(recipe["verify"], {**ctx.variables, "artifact": artifact}, ctx.project, ctx.env)
        return {"level": "recipe", "checks": ["explicit recipe verifier exited successfully"],
                "scope": "semantics depend on the user-supplied verifier"}
    result = verify_portable(target, artifact, ctx.package)
    if target == "crate" and any(record["argv"][:2] == ["cargo", "package"] and record["returncode"] == 0
                                 for record in ctx.runner.records):
        result["checks"].append("cargo package performed its default unpack-and-build verification")
    return result
