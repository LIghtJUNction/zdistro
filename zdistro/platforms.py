"""Small native adapters: artifacts are built by the platform's own tools."""

from __future__ import annotations

import hashlib
import csv
import io
import json
from pathlib import Path
import shlex
import shutil
import struct
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .model import DistroError, host_name, inside, relative_path, text
from .verify import verify_portable


def write_xml(path, root):
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def wix_source(ctx):
    package = ctx.package
    root = ET.Element("Wix", xmlns="http://wixtoolset.org/schemas/v4/wxs")
    record = ET.SubElement(root, "Package", Name=package["name"],
                           Manufacturer=package.get("maintainer", package["name"]),
                           Version=package["version"], Scope="perMachine",
                           UpgradeCode=str(uuid.uuid5(uuid.NAMESPACE_URL, package.get("identifier", package["name"]))))
    ET.SubElement(record, "MediaTemplate", EmbedCab="yes")
    ET.SubElement(record, "MajorUpgrade", DowngradeErrorMessage="A newer version is already installed.")
    standard = ET.SubElement(record, "StandardDirectory", Id="ProgramFiles6432Folder")
    install = ET.SubElement(standard, "Directory", Id="INSTALLFOLDER", Name=package["name"])
    feature = ET.SubElement(record, "Feature", Id="MainFeature", Title=package["name"], Level="1")
    directories = {".": install}
    for file in sorted(ctx.stage.rglob("*")):
        if not file.is_file():
            continue
        relative = file.relative_to(ctx.stage)
        for parent in reversed(relative.parents):
            key = parent.as_posix()
            if key not in directories:
                parent_node = directories[parent.parent.as_posix()]
                directories[key] = ET.SubElement(parent_node, "Directory", Name=parent.name,
                                                Id="D" + hashlib.sha256(key.encode()).hexdigest()[:24])
        token = hashlib.sha256(relative.as_posix().encode()).hexdigest()[:24]
        component = ET.SubElement(directories[relative.parent.as_posix()], "Component", Id="C" + token, Guid="*")
        ET.SubElement(component, "File", Id="F" + token, Source=str(file), KeyPath="yes")
        ET.SubElement(feature, "ComponentRef", Id="C" + token)
    path = ctx.work / "package.wxs"
    write_xml(path, root)
    return path


def solid_png(path, size):
    """Generate a neutral template asset; applications can provide their own."""
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))
    rows = (b"\0" + bytes((66, 133, 244, 255)) * size) * size
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def msix_manifest(ctx):
    package, config = ctx.package, ctx.config
    executable = str(relative_path(config["executable"], "MSIX executable"))
    exe = inside(ctx.stage, executable)
    if not exe.is_file():
        raise DistroError("MSIX executable must be a staged Windows PE file")
    with exe.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise DistroError("MSIX executable must be a staged Windows PE file")
    path = ctx.stage / "AppxManifest.xml"
    if path.exists():
        raise DistroError("AppxManifest.xml is reserved for generated MSIX metadata")
    assets = ctx.stage / "ZdistroAssets"
    if assets.exists():
        raise DistroError("ZdistroAssets is reserved for the generated MSIX template icons")
    assets.mkdir()
    for name, size in (("StoreLogo.png", 50), ("Logo150.png", 150), ("Logo44.png", 44)):
        solid_png(assets / name, size)
    root = ET.Element("Package", {"xmlns": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
        "xmlns:uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
        "xmlns:rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities",
        "IgnorableNamespaces": "uap rescap"})
    version = config.get("version", package["version"] + ".0" if package["version"].count(".") == 2 else package["version"])
    ET.SubElement(root, "Identity", Name=config.get("identity", package["identifier"]), Publisher=config.get("publisher", "CN=Package Publisher"),
                  Version=version, ProcessorArchitecture=config.get("architecture", "x64"))
    properties = ET.SubElement(root, "Properties")
    for key, value in (("DisplayName", package["name"]), ("PublisherDisplayName", package.get("maintainer", package["name"])),
                       ("Logo", "ZdistroAssets/StoreLogo.png")):
        ET.SubElement(properties, key).text = value
    dependencies = ET.SubElement(root, "Dependencies")
    ET.SubElement(dependencies, "TargetDeviceFamily", Name="Windows.Desktop", MinVersion="10.0.17763.0", MaxVersionTested="10.0.26100.0")
    ET.SubElement(ET.SubElement(root, "Resources"), "Resource", Language=config.get("language", "en-US"))
    app = ET.SubElement(ET.SubElement(root, "Applications"), "Application", Id="App",
                        Executable=executable.replace("/", "\\"), EntryPoint="Windows.FullTrustApplication")
    ET.SubElement(app, "uap:VisualElements", DisplayName=package["name"], Description=package["description"],
                  BackgroundColor="transparent", Square150x150Logo="ZdistroAssets/Logo150.png", Square44x44Logo="ZdistroAssets/Logo44.png")
    ET.SubElement(ET.SubElement(root, "Capabilities"), "rescap:Capability", Name="runFullTrust")
    write_xml(path, root)


def build_platform(ctx, artifact):
    from .backends import android_platform, android_tool, bundletool_command, windows_tool
    target, config, package = ctx.target, ctx.config, ctx.package
    if target == "pkg":
        ctx.run(["pkgbuild", "--root", ctx.stage, "--identifier", package["identifier"],
                 "--version", package["version"], "--install-location", "/", artifact])
    elif target == "dmg":
        ctx.run(["hdiutil", "create", "-srcfolder", ctx.stage, "-volname", package["name"], "-format", "UDZO", artifact])
    elif target == "msi":
        source = wix_source(ctx)
        ctx.run(["wix", "build", "-arch", config.get("architecture", "x64"), "-o", artifact, source])
    elif target == "msix":
        msix_manifest(ctx)
        ctx.run([windows_tool("makeappx"), "pack", "/d", ctx.stage, "/p", artifact])
    elif target in ("apk", "aab"):
        mode = config.get("mode", "aapt2" if target == "apk" else "gradle")
        if mode == "aapt2":
            aapt2 = android_tool("aapt2")
            linked = artifact if target == "apk" else ctx.work / "linked-proto.zip"
            argv = [aapt2, "link", "-I", android_platform(config), "--manifest",
                    inside(ctx.project, config.get("manifest", "AndroidManifest.xml")),
                    "-o", linked, "--auto-add-overlay"]
            if target == "aab":
                argv.append("--proto-format")
            if config.get("resources"):
                compiled = ctx.work / "resources.zip"
                ctx.run([aapt2, "compile", "--dir", inside(ctx.project, config["resources"]), "-o", compiled])
                argv.extend(["-R", compiled])
            ctx.run(argv)
            if target == "aab":
                module = ctx.work / "base.zip"
                with zipfile.ZipFile(linked) as original, zipfile.ZipFile(module, "x", compression=zipfile.ZIP_DEFLATED) as base:
                    for name in original.namelist():
                        destination = "manifest/AndroidManifest.xml" if name == "AndroidManifest.xml" else name
                        with original.open(name) as source, base.open(destination, "w") as output:
                            shutil.copyfileobj(source, output)
                packaged = module
            else:
                packaged = artifact
            with zipfile.ZipFile(packaged, "a", compression=zipfile.ZIP_DEFLATED) as archive:
                names = set(archive.namelist())
                for file in sorted(ctx.stage.rglob("*")):
                    if file.is_file():
                        name = file.relative_to(ctx.stage).as_posix()
                        if name in names:
                            raise DistroError(f"staged APK file collides with AAPT2 output: {name}")
                        archive.write(file, name)
            if target == "aab":
                ctx.run([*bundletool_command(config), "build-bundle", f"--modules={module}", f"--output={artifact}"])
        else:
            wrapper = ctx.project / ("gradlew.bat" if host_name() == "windows" else "gradlew")
            executable = str(wrapper) if wrapper.is_file() else "gradle"
            task = text(config.get("task"), "Gradle task", "assembleRelease" if target == "apk" else "bundleRelease")
            ctx.run([executable, "--no-daemon", task], ctx.project)
            source = inside(ctx.project, config["artifact"], "Gradle artifact")
            if not source.is_file():
                raise DistroError("Gradle succeeded but its declared artifact is missing")
            shutil.copyfile(source, artifact)
    elif target == "docker":
        engine = config.get("engine", "docker")
        layout = config.get("archive", "docker")
        dockerfile = inside(ctx.project, config.get("dockerfile", "Dockerfile"))
        tag = f"zdistro-local/{package['name'].lower()}:{uuid.uuid4().hex}"
        flags = ["--platform", config["platform"]] if config.get("platform") else []
        if engine == "docker":
            output = io.StringIO()
            csv.writer(output, lineterminator="").writerow(["type=" + layout, "dest=" + str(artifact)])
            ctx.run([engine, "buildx", "build", "--file", dockerfile, *flags,
                     "--output", output.getvalue(), ctx.project], ctx.project)
        else:
            ctx.run([engine, "build", "--file", dockerfile, "--tag", tag, *flags, ctx.project], ctx.project)
            try:
                ctx.run([engine, "image", "inspect", tag, "--format", "{{.Id}}"], ctx.project)
                ctx.run([engine, "save", "--format", "oci-archive" if layout == "oci" else "docker-archive",
                         "--output", artifact, tag])
            finally:
                ctx.run([engine, "image", "rm", tag], ctx.project)
    return artifact


def verify_platform(ctx, artifact):
    from .backends import android_tool, bundletool_command, windows_tool
    target = ctx.target
    checks = []
    identity = None
    if target in ("msix", "apk", "aab"):
        checks.extend(verify_portable(target, artifact, ctx.package)["checks"])
    if target == "pkg":
        ctx.run(["pkgutil", "--expand-full", artifact, ctx.work / "pkg-expanded"])
        checks.append("pkgutil expanded native metadata and payload without installing")
    elif target == "dmg":
        ctx.run(["hdiutil", "verify", artifact])
        ctx.run(["hdiutil", "imageinfo", artifact])
        checks.append("hdiutil image integrity and format inspection without mounting")
    elif target == "msi":
        ctx.run(["wix", "msi", "decompile", artifact, "-o", ctx.work / "verified.wxs"])
        checks.append("WiX decoded the native MSI database; installation and ICE validation not performed")
    elif target == "msix":
        ctx.run([windows_tool("makeappx"), "unpack", "/p", artifact, "/d", ctx.work / "msix-expanded"])
        checks.append("MakeAppx native unpack; package is unsigned")
    elif target == "apk":
        badging = ctx.run([android_tool("aapt2"), "dump", "badging", artifact])
        package_line = next((line for line in badging.splitlines() if line.startswith("package: ")), "")
        fields = dict(item.split("=", 1) for item in shlex.split(package_line)[1:] if "=" in item)
        identity = {"application_id": fields.get("name"), "version_name": fields.get("versionName"),
                    "version_code": fields.get("versionCode")}
        checks.append("AAPT2 parsed binary Android manifest/resources; signing not performed")
    elif target == "aab":
        command = bundletool_command(ctx.config)
        if not command:
            raise DistroError("bundletool is required for AAB verification")
        ctx.run([*command, "validate", f"--bundle={artifact}"])
        manifest = ET.fromstring(ctx.run([*command, "dump", "manifest", f"--bundle={artifact}", "--module=base"]))
        android = "{http://schemas.android.com/apk/res/android}"
        identity = {"application_id": manifest.get("package"), "version_name": manifest.get(android + "versionName"),
                    "version_code": manifest.get(android + "versionCode")}
        checks.append("bundletool validated Android App Bundle modules")
    result = {"level": "native", "checks": checks}
    if identity is not None:
        expected = {"application_id": ctx.config.get("application_id", ctx.package.get("identifier")),
                    "version_name": ctx.config.get("version_name", ctx.package["version"])}
        if "version_code" in ctx.config:
            expected["version_code"] = str(ctx.config["version_code"])
        for key, value in expected.items():
            if not value or identity[key] != value:
                raise DistroError(f"Android {key} differs from manifest: expected {value!r}, artifact contains {identity[key]!r}")
        checks.append("Android application ID and version match explicit manifest expectations")
        result["identity"] = identity
    return result
