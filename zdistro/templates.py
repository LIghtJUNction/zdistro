"""Editable, offline templates; SDKs, credentials and signatures are not invented."""

import json
from pathlib import Path
import re
from xml.sax.saxutils import escape

from .backends import BACKENDS
from .model import DistroError


ALIASES = {"ubuntu": "deb", "debian": "deb", "archlinux": "arch", "tar.gz": "tar-gz",
           "python": "wheel", "rust": "crate", "chrome": "extension", "firefox": "xpi",
           "android": "apk", "macos": "pkg", "windows": "msi", "oci": "docker",
           "安卓": "apk", "浏览器插件": "extension", "火狐插件": "xpi", "容器": "docker",
           "苹果": "pkg", "压缩包": "zip", "乌班图": "deb"}


def targets_from(values):
    result = []
    for group in values or []:
        for value in group.split(","):
            value = ALIASES.get(value.strip().lower(), value.strip().lower())
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
                raise DistroError(f"unknown target identifier or alias: {value!r}")
            if value not in result:
                result.append(value)
    return result


def initialize(directory, targets, name=None, version="0.1.0"):
    directory = Path(directory).resolve()
    path = directory / "zdistro.toml"
    if path.exists():
        raise DistroError(f"manifest already exists; edit it rather than overwriting: {path}")
    name = name or re.sub(r"[^a-z0-9.-]+", "-", directory.name.lower()).strip("-.") or "my-package"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise DistroError("template name must be a lowercase package name; version must be X.Y.Z")
    if not targets:
        raise DistroError("choose at least one --target")
    identifier = "dev.projectz." + re.sub(r"[-_.]+", ".", name)
    q = lambda value: json.dumps(value, ensure_ascii=False)
    files = {"payload/hello.txt": f"Hello from {name}.\n"}
    lines = ["# Edit metadata, source paths and per-format settings before distributing.", "schema = 1",
             "source_date_epoch = 1700000000", "", "[package]", f"name = {q(name)}", f"version = {q(version)}",
             'description = "A package created with zdistro"', 'maintainer = "Package Maintainer <maintainer@example.invalid>"',
             'license = "UNLICENSED"', f"identifier = {q(identifier)}", "", "[build]", 'kind = "none"', "",
             "[[files]]", 'source = "payload/hello.txt"', f'destination = "usr/local/share/{name}/hello.txt"', ""]
    for target in targets:
        lines.append(f"[targets.{target}]")
        if target == "deb":
            lines.append('architecture = "all"')
        elif target == "arch":
            lines.extend(['architecture = "any"', 'release = "1"'])
        elif target in ("wheel", "sdist"):
            lines.extend(['project = "python"', "files = []"])
            files["python/pyproject.toml"] = f'''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
[project]
name = {q(name)}
version = {q(version)}
description = "A zdistro PEP 517 example"
requires-python = ">=3.11"
[tool.setuptools]
py-modules = ["hello"]
'''
            files["python/hello.py"] = 'def hello():\n    return "Hello from Python"\n'
        elif target == "crate":
            lines.extend(['project = "rust"', "files = []"])
            files["rust/Cargo.toml"] = f'''[package]
name = {q(name)}
version = {q(version)}
edition = "2021"
description = "A zdistro Cargo example"
license = "MIT"
[lib]
path = "lib.rs"
[workspace]
'''
            files["rust/lib.rs"] = '#![no_std]\npub const fn answer() -> u32 { 42 }\n'
            files["rust/README.md"] = "# Cargo packaging example\n\nGenerated for a packaging smoke test.\n"
        elif target == "npm":
            lines.extend(['project = "npm"', "files = []"])
            files["npm/package.json"] = json.dumps({"name": name, "version": version, "description": "A zdistro npm example",
                                                    "license": "UNLICENSED", "main": "index.js", "files": ["index.js"]}, indent=2) + "\n"
            files["npm/index.js"] = 'module.exports = () => "Hello from JavaScript";\n'
        elif target in ("extension", "xpi"):
            lines.append('files = [{source = "extension", destination = "."}]')
            files["extension/manifest.json"] = json.dumps({"manifest_version": 3, "name": name, "version": version,
                "description": "A minimal zdistro packaging example", "action": {"default_popup": "popup.html"}}, indent=2) + "\n"
            files["extension/popup.html"] = '<!doctype html><html><meta charset="utf-8"><title>Package example</title><p>Hello from zdistro.</p></html>\n'
        elif target in ("apk", "aab"):
            lines.extend(['project = "android"', "files = []"])
            files["android/AndroidManifest.xml"] = f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{escape(identifier)}" android:versionCode="1" android:versionName="{escape(version)}">
  <uses-sdk android:minSdkVersion="23" android:targetSdkVersion="35" />
  <application android:label="{escape(name)}" android:hasCode="false" />
</manifest>
'''
            if target == "apk":
                lines.extend(['mode = "aapt2"', 'manifest = "AndroidManifest.xml"', '# Optional: resources = "res"'])
            else:
                lines.extend(['mode = "gradle"', 'task = ":app:bundleRelease"',
                              'artifact = "app/build/outputs/bundle/release/app-release.aab"',
                              '# bundletool_jar = "/path/to/bundletool-all.jar"'])
                files["android/README.md"] = ("# Android source\n\nThe APK example packages a no-code Android manifest with an installed AAPT2 and SDK platform.\n"
                    "For AAB, point targets.aab.project at your real Gradle Android app with its wrapper,\n"
                    "select its task/output, and provide bundletool. No SDK or Gradle wrapper is downloaded by init.\n")
        elif target == "msix":
            lines.extend(['architecture = "x64"', 'executable = "app.exe"', 'publisher = "CN=Package Publisher"',
                          'files = [{source = "payload/app.exe", destination = "app.exe"}]'])
        elif target == "msi":
            lines.extend(['architecture = "x64"', 'files = [{source = "payload/hello.txt", destination = "hello.txt"}]'])
        elif target == "docker":
            lines.extend(['project = "container"', 'engine = "docker"', 'archive = "docker"', "files = []"])
            files["container/Dockerfile"] = 'FROM scratch\nCOPY hello.txt /hello.txt\nLABEL org.opencontainers.image.title="zdistro example"\n'
            files["container/hello.txt"] = "A real image archive without a downloaded base image.\n"
        elif target not in BACKENDS:
            lines.extend(["", f"[recipes.{target}]", 'hosts = ["linux", "macos", "windows"]',
                          'tools = ["your-packager", "your-verifier"]', f'artifact = "{name}-{version}.{target}"',
                          'build = [["your-packager", "--input", "{stage}", "--output", "{artifact}"]]',
                          'verify = [["your-verifier", "{artifact}"]]'])
        lines.append("")
    directory.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        destination = directory / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(lines))
    return path
