"""Manifest and staging rules shared by every builder and package backend."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import string
import tomllib


class DistroError(RuntimeError):
    """A reviewable configuration, prerequisite, build or verification failure."""


def host_name():
    return {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system(), platform.system().lower())


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(value, label="path", allow_dot=False):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise DistroError(f"{label} must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".git") for part in path.parts) or (str(path) == "." and not allow_dot):
        raise DistroError(f"{label} escapes its boundary: {value!r}")
    return path


def inside(base, value, label="path", allow_dot=True):
    relative = relative_path(value, label, allow_dot)
    candidate = (base / str(relative)).resolve()
    if not candidate.is_relative_to(base.resolve()):
        raise DistroError(f"{label} resolves outside its source directory: {value}")
    return candidate


def text(value, label, default=None):
    if value is None and default is not None:
        value = default
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise DistroError(f"{label} must be a nonempty single-line string")
    return value


def string_list(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) or "\x00" in item for item in value):
        raise DistroError(f"{label} must be a list of strings")
    return value


def commands(value, label):
    if not isinstance(value, list) or any(not isinstance(row, list) or not row for row in value):
        raise DistroError(f"{label} must contain nonempty argv arrays")
    for row in value:
        string_list(row, label)
    return value


def expand(value, variables):
    if not isinstance(value, str):
        raise DistroError("command arguments must be strings")
    for _, field, spec, conversion in string.Formatter().parse(value):
        if field is not None and (field not in variables or spec or conversion):
            raise DistroError(f"unknown or formatted placeholder: {field!r}")
    try:
        return value.format_map({key: str(item) for key, item in variables.items()})
    except (KeyError, ValueError) as error:
        raise DistroError(f"invalid argument placeholder: {error}") from error


@dataclass(frozen=True)
class Manifest:
    path: Path
    data: dict
    sha256: str

    @property
    def root(self):
        return self.path.parent

    @property
    def package(self):
        return self.data["package"]

    @property
    def targets(self):
        return self.data["targets"]

    @classmethod
    def load(cls, filename):
        path = Path(filename).resolve()
        try:
            raw = path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as error:
            raise DistroError(f"cannot read manifest {path}: {error}") from error
        if data.get("schema") != 1:
            raise DistroError("manifest schema must be 1")
        package = data.get("package")
        if not isinstance(package, dict):
            raise DistroError("missing [package] table")
        for field in ("name", "version", "description"):
            text(package.get(field), f"package.{field}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", package["name"]):
            raise DistroError("package.name must contain only letters, digits, dot, underscore or hyphen")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+~:_-]*", package["version"]):
            raise DistroError("package.version contains unsupported filename characters")
        for field in ("maintainer", "license", "homepage", "identifier"):
            if field in package:
                text(package[field], f"package.{field}")
        targets = data.get("targets")
        if not isinstance(targets, dict) or not targets:
            raise DistroError("declare at least one [targets.<format>] table")
        for key, config in targets.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", key) or not isinstance(config, dict):
                raise DistroError("target names must be lowercase identifiers with table values")
        build = data.get("build", {})
        if not isinstance(build, dict) or build.get("kind", "none") not in ("none", "cargo", "python", "npm", "command"):
            raise DistroError("build.kind must be none, cargo, python, npm or command")
        if build.get("kind") == "command":
            commands(build.get("commands"), "build.commands")
        relative_path(build.get("project", "."), "build.project", True)
        recipes = data.get("recipes", {})
        if not isinstance(recipes, dict):
            raise DistroError("recipes must be a table")
        for key, recipe in recipes.items():
            if not isinstance(recipe, dict):
                raise DistroError(f"recipes.{key} must be a table")
            commands(recipe.get("build"), f"recipes.{key}.build")
            commands(recipe.get("verify"), f"recipes.{key}.verify")
            if not recipe["build"] or not recipe["verify"]:
                raise DistroError("custom recipes need both build and verify commands")
            string_list(recipe.get("tools", []), f"recipes.{key}.tools")
            string_list(recipe.get("hosts", ["linux", "macos", "windows"]), f"recipes.{key}.hosts")
            relative_path(recipe.get("artifact", ""), f"recipes.{key}.artifact")
        manifest = cls(path, data, hashlib.sha256(raw).hexdigest())
        for config in targets.values():
            manifest.file_rules(config)
        return manifest

    def file_rules(self, config):
        rules = config.get("files", self.data.get("files", []))
        if not isinstance(rules, list):
            raise DistroError("files must be an array of tables")
        for rule in rules:
            if not isinstance(rule, dict):
                raise DistroError("each file rule must be a table")
            source = rule.get("source", "")
            if isinstance(source, str) and source.startswith("{build}/"):
                relative_path(source[len("{build}/"):], "files.source")
            else:
                relative_path(source, "files.source", True)
            relative_path(rule.get("destination", ""), "files.destination", True)
            if "mode" in rule and not re.fullmatch(r"0?[0-7]{3}", str(rule["mode"])):
                raise DistroError("file mode must be three octal permission digits, without special bits")
        return rules


def copy_project(source, destination, excluded=()):
    """Copy declared source without VCS, caches or this build's output tree."""
    source = source.resolve()
    if not source.is_dir():
        raise DistroError(f"project directory is missing: {source}")
    excluded = [Path(item).resolve() for item in excluded]
    ignored = {".git", ".venv", "venv", "__pycache__", ".zdistro", "node_modules", "target"}
    def ignore(parent, names):
        result = []
        for name in names:
            candidate = Path(parent) / name
            if name in ignored or any(candidate.resolve() == item for item in excluded):
                result.append(name)
            elif candidate.is_symlink():
                raise DistroError(f"source symlinks require an explicit external recipe: {candidate}")
        return result
    shutil.copytree(source, destination, ignore=ignore)
    return destination


def stage_files(manifest, config, build_dir, stage, excluded=()):
    stage.mkdir(parents=True, exist_ok=False)
    seen = set()
    rows = []
    for rule in manifest.file_rules(config):
        value = rule["source"]
        if value.startswith("{build}/"):
            source = inside(build_dir, value[len("{build}/"):], "build file")
        else:
            source = inside(manifest.root, value, "source file")
        if not source.exists():
            raise DistroError(f"source file is missing: {value}")
        destination = relative_path(rule["destination"], "destination", True)
        paths = sorted(source.rglob("*")) if source.is_dir() else [source]
        for item in paths:
            if item.is_symlink():
                raise DistroError(f"staging symlinks require an explicit recipe: {item}")
            if any(item.resolve().is_relative_to(Path(excluded_path).resolve()) for excluded_path in excluded):
                raise DistroError("a file rule includes the build output directory; narrow files.source")
            if item.is_dir():
                continue
            if not item.is_file():
                raise DistroError(f"only regular files can be staged: {item}")
            rel = destination / item.relative_to(source).as_posix() if source.is_dir() else destination
            relative_path(str(rel), "staged file")
            key = str(rel).casefold()
            if key in seen:
                raise DistroError(f"overlapping or case-colliding staged paths: {rel}")
            seen.add(key)
            output = stage / str(rel)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, output)
            mode = int(str(rule["mode"]), 8) if "mode" in rule else stat.S_IMODE(item.stat().st_mode) & 0o777
            output.chmod(mode)
            rows.append({"path": str(rel), "size": output.stat().st_size, "sha256": digest(output), "mode": oct(mode)})
    return rows


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)
