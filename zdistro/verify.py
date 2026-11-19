"""Inspect artifacts without installing them or extracting untrusted paths."""

from __future__ import annotations

import base64
import csv
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tomllib
import zipfile

from .model import DistroError, relative_path


def zip_contents(path):
    try:
        archive = zipfile.ZipFile(path)
        names = archive.namelist()
        for name in names:
            relative_path(name.rstrip("/"), "archive member", True)
        if len(names) != len(set(names)):
            raise DistroError("archive contains duplicate member names")
        bad = archive.testzip()
        if bad is not None:
            raise DistroError(f"ZIP checksum failed: {bad}")
        return archive
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise DistroError(f"invalid ZIP artifact: {error}") from error


def tar_contents(path):
    try:
        archive = tarfile.open(path, "r:*")
        for member in archive.getmembers():
            relative_path(member.name.rstrip("/"), "archive member", True)
            if member.issym() or member.islnk():
                relative_path(member.linkname, "archive link", True)
            elif not (member.isfile() or member.isdir()):
                raise DistroError(f"unexpected archive device or special entry: {member.name}")
        return archive
    except (OSError, tarfile.TarError) as error:
        raise DistroError(f"invalid tar artifact: {error}") from error


def read_tar(archive, name):
    try:
        stream = archive.extractfile(name)
        if stream is None:
            raise DistroError(f"not a regular archive file: {name}")
        with stream:
            return stream.read()
    except KeyError as error:
        raise DistroError(f"missing archive member: {name}") from error


def check_extension(manifest, names):
    if not isinstance(manifest, dict) or manifest.get("manifest_version") not in (2, 3):
        raise DistroError("extension manifest_version must be 2 or 3")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise DistroError("extension needs a name")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", version):
        raise DistroError("extension version must have one to four numeric components")
    if any(int(part) > 65535 for part in version.split(".")):
        raise DistroError("extension version components exceed 65535")
    paths = []
    icons = manifest.get("icons", {})
    if not isinstance(icons, dict):
        raise DistroError("extension icons must be a map")
    paths.extend(icons.values())
    for field in ("action", "browser_action", "page_action", "options_ui", "sidebar_action"):
        row = manifest.get(field, {})
        if not isinstance(row, dict):
            raise DistroError(f"extension {field} must be a map")
        paths.extend(row[key] for key in ("default_popup", "page", "default_panel") if key in row)
    background = manifest.get("background", {})
    if not isinstance(background, dict):
        raise DistroError("extension background must be a map")
    paths.extend(background.get("scripts", []))
    if "service_worker" in background:
        paths.append(background["service_worker"])
    scripts = manifest.get("content_scripts", [])
    if not isinstance(scripts, list):
        raise DistroError("content_scripts must be an array")
    for row in scripts:
        if not isinstance(row, dict):
            raise DistroError("content_scripts entries must be maps")
        paths.extend(row.get("js", []))
        paths.extend(row.get("css", []))
    for path in paths:
        relative_path(path, "extension resource")
        if path not in names:
            raise DistroError(f"extension resource is missing: {path}")


def metadata_matches(raw, name=None, version=None):
    metadata = BytesParser().parsebytes(raw)
    if not metadata.get("Name") or not metadata.get("Version"):
        raise DistroError("Python artifact is missing Name or Version metadata")
    normalize = lambda item: re.sub(r"[-_.]+", "-", item).lower()
    if name and normalize(metadata["Name"]) != normalize(name):
        raise DistroError("Python artifact name differs from the manifest")
    if version and metadata["Version"] != version:
        raise DistroError("Python artifact version differs from the manifest")
    return metadata


def verify_portable(kind, path, package):
    """Structural/integrity verification; no browser/store/install claim."""
    name, version = package["name"], package["version"]
    checks = []
    if kind in ("zip", "extension", "xpi", "wheel", "msix", "apk", "aab"):
        with zip_contents(path) as archive:
            names = archive.namelist()
            checks.append("ZIP member boundaries and CRCs")
            if kind in ("extension", "xpi"):
                if "manifest.json" not in names:
                    raise DistroError("extension manifest.json must be at the archive root")
                check_extension(json.loads(archive.read("manifest.json")), set(names))
                checks.append("extension required fields and declared local scripts/pages/icons")
            elif kind == "wheel":
                metadata = [item for item in names if item.endswith(".dist-info/METADATA")]
                if len(metadata) != 1:
                    raise DistroError("wheel must contain one dist-info/METADATA")
                prefix = metadata[0].rsplit("/", 1)[0]
                metadata_matches(archive.read(metadata[0]), name, version)
                if prefix + "/WHEEL" not in names or prefix + "/RECORD" not in names:
                    raise DistroError("wheel is missing WHEEL or RECORD")
                recorded = set()
                for member, hash_field, size in csv.reader(io.StringIO(archive.read(prefix + "/RECORD").decode())):
                    if member not in names or member in recorded:
                        raise DistroError(f"invalid wheel RECORD member: {member}")
                    recorded.add(member)
                    if member == prefix + "/RECORD" and not hash_field and not size:
                        continue
                    algorithm, _, encoded = hash_field.partition("=")
                    if algorithm not in ("sha256", "sha384", "sha512"):
                        raise DistroError("wheel RECORD must use a supported strong digest")
                    content = archive.read(member)
                    actual = base64.urlsafe_b64encode(hashlib.new(algorithm, content).digest()).decode().rstrip("=")
                    if encoded != actual or size != str(len(content)):
                        raise DistroError(f"wheel RECORD integrity failed: {member}")
                if set(item for item in names if not item.endswith("/")) != recorded:
                    raise DistroError("wheel contains files absent from RECORD")
                checks.append("Python metadata and every wheel RECORD digest/size")
            elif kind == "msix":
                if not {"AppxManifest.xml", "AppxBlockMap.xml", "[Content_Types].xml"} <= set(names):
                    raise DistroError("MSIX is missing native package metadata")
            elif kind == "apk" and "AndroidManifest.xml" not in names:
                raise DistroError("APK is missing AndroidManifest.xml")
            elif kind == "aab" and not {"BundleConfig.pb", "base/manifest/AndroidManifest.xml"} <= set(names):
                raise DistroError("AAB is missing bundle or base-module metadata")
    else:
        with tar_contents(path) as archive:
            names = archive.getnames()
            checks.append("tar member boundaries and readable archive")
            if kind == "crate":
                candidates = [item for item in names if item.endswith("/Cargo.toml") and item.count("/") == 1]
                if len(candidates) != 1:
                    raise DistroError("crate must contain a root Cargo.toml")
                record = tomllib.loads(read_tar(archive, candidates[0]).decode())
                if record.get("package", {}).get("name") != name or record["package"].get("version") != version:
                    raise DistroError("crate metadata differs from the manifest")
                checks.append("Cargo-normalized package name and version")
            elif kind == "sdist":
                candidates = [item for item in names if item.endswith("/PKG-INFO") and item.count("/") == 1]
                if len(candidates) != 1:
                    raise DistroError("sdist must contain root PKG-INFO")
                metadata_matches(read_tar(archive, candidates[0]), name, version)
                checks.append("PEP 517 source distribution metadata")
            elif kind == "npm":
                record = json.loads(read_tar(archive, "package/package.json"))
                if record.get("name") != name or record.get("version") != version:
                    raise DistroError("npm metadata differs from the manifest")
                checks.append("npm package name and version")
            elif kind == "docker":
                checks.extend(verify_image_archive(archive))
    return {"level": "structure", "checks": checks}


def verify_image_archive(archive):
    names = set(archive.getnames())
    if "oci-layout" in names:
        layout = json.loads(read_tar(archive, "oci-layout"))
        if layout.get("imageLayoutVersion") != "1.0.0":
            raise DistroError("unsupported OCI image layout version")
        index = json.loads(read_tar(archive, "index.json"))
        pending = list(index.get("manifests", []))
        if not pending:
            raise DistroError("OCI archive contains no image manifests")
        seen = set()
        while pending:
            descriptor = pending.pop()
            digest = descriptor.get("digest", "")
            if digest in seen:
                continue
            seen.add(digest)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise DistroError("OCI verification currently requires SHA-256 descriptors")
            raw = read_tar(archive, "blobs/sha256/" + digest.split(":")[1])
            if hashlib.sha256(raw).hexdigest() != digest.split(":")[1] or len(raw) != descriptor.get("size"):
                raise DistroError("OCI descriptor digest or size mismatch")
            media = descriptor.get("mediaType", "")
            if "manifest" in media or "index" in media:
                document = json.loads(raw)
                pending.extend(document.get("manifests", []))
                pending.extend(document.get("layers", []))
                if "config" in document:
                    pending.append(document["config"])
        return ["OCI index and recursively referenced blob digests/sizes"]
    if "manifest.json" not in names:
        raise DistroError("not a Docker or OCI image archive")
    manifest = json.loads(read_tar(archive, "manifest.json"))
    if not isinstance(manifest, list) or not manifest:
        raise DistroError("Docker archive contains no images")
    for image in manifest:
        config = read_tar(archive, image["Config"])
        json.loads(config)
        config_digest = Path(image["Config"]).stem
        if re.fullmatch(r"[0-9a-f]{64}", config_digest) and hashlib.sha256(config).hexdigest() != config_digest:
            raise DistroError("Docker configuration digest mismatch")
        for layer in image.get("Layers", []):
            read_tar(archive, layer)
    return ["Docker image manifest, configuration and referenced layers"]
