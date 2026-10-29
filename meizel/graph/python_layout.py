# meiva:note
# Native import names for independently packaged projects come from their explicit
# wheel metadata. Aliases retain physical source paths so normal graph rules apply.

from __future__ import annotations

import keyword
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TypeGuard

from meizel.graph.model import MeizelError

__all__ = ["StandaloneProject", "project_metadata_paths", "standalone_projects"]


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def _table(value: object, key: str) -> object:
    return value.get(key) if _is_table(value) else None


# meiva:note
# The manifest parser owns malformed root declarations; layout discovery defers to it.
def _standalone_roots(text: str) -> tuple[str, ...]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ()
    roots = _table(_table(document, "roles"), "standalone")
    if not _is_str_list(roots):
        return ()
    return tuple(roots)


def _packages(path: str, text: str) -> list[str]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise MeizelError(f"{path}: invalid standalone project metadata: {error}") from error
    if _table(_table(document, "build-system"), "build-backend") != "hatchling.build":
        raise MeizelError(f"{path}: standalone source requires explicit Hatch wheel.packages")
    build = _table(_table(document, "tool"), "hatch")
    build = _table(build, "build")
    wheel = _table(_table(build, "targets"), "wheel")
    for options in (build, wheel):
        if _is_table(options) and any(
            key in options for key in ("sources", "force-include", "only-include")
        ):
            raise MeizelError(f"{path}: standalone wheel source rewrites are unsupported")
    value = _table(wheel, "packages")
    if not _is_str_list(value) or not value:
        raise MeizelError(f"{path}: standalone source requires nonempty Hatch wheel.packages")
    return value


def _valid_package_path(package: str) -> bool:
    relative = PurePosixPath(package)
    return (
        package == relative.as_posix()
        and not relative.is_absolute()
        and not any(part in {".", ".."} for part in relative.parts)
        and "\\" not in package
        and bool(relative.parts)
    )


def _package_path(project: str, package: str, files: frozenset[str]) -> tuple[str, str]:
    name = PurePosixPath(package).name
    valid_name = name.isascii() and name.isidentifier() and not keyword.iskeyword(name)
    if not _valid_package_path(package) or not valid_name:
        raise MeizelError(f"{project}/pyproject.toml: invalid wheel package path {package!r}")
    if name in sys.stdlib_module_names or name in sys.builtin_module_names:
        raise MeizelError(
            f"{project}: native package alias {name!r} collides with the standard library"
        )
    physical = f"{project}/{package}"
    if not any(path.startswith(physical + "/") and path.endswith(".py") for path in files):
        raise MeizelError(
            f"{project}/pyproject.toml: wheel package {package!r} has no Python source"
        )
    return name, physical


def project_metadata_paths(files: frozenset[str], root_manifest: str) -> tuple[str, ...]:
    roots = _standalone_roots(root_manifest)
    return tuple(
        sorted(
            path
            for path in files
            if path.endswith("/pyproject.toml")
            and any(path.startswith(root + "/") for root in roots)
        )
    )


# meiva:note
# The one metadata owner for native module names and the separately checked project.
@dataclass(frozen=True, slots=True)
class StandaloneProject:
    directory: str
    packages: tuple[tuple[str, str], ...]


def _relative_directory(project: str, value: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts)
        or any(char in value for char in "*?[]\\")
    ):
        raise MeizelError(f"{project}: standalone checks require relative directory paths")
    return value


def _check_test_sources(project: str, paths: list[str], files: frozenset[str]) -> None:
    for target in paths:
        prefix = f"{project}/{target}/"
        if not any(name.startswith(prefix) and name.endswith(".py") for name in files):
            raise MeizelError(f"{project}: pytest testpath {target!r} has no Python source")


def _check_source_includes(project: str, selected: list[str], files: frozenset[str]) -> None:
    for path in files:
        if not path.startswith(project + "/") or not path.endswith(".py"):
            continue
        local = path.removeprefix(project + "/")
        if not any(local.startswith(item + "/") for item in selected):
            raise MeizelError(f"{project}: strict typing does not include source {local!r}")


def _check_coverage(
    project: str, document: object, packages: list[str], files: frozenset[str]
) -> None:
    tool = _table(document, "tool")
    checker = _table(tool, "basedpyright")
    paths = _table(_table(_table(tool, "pytest"), "ini_options"), "testpaths")
    includes = _table(checker, "include")
    if not _is_str_list(paths) or not paths or not _is_str_list(includes) or not includes:
        raise MeizelError(f"{project}: standalone checks require explicit include and testpaths")
    selected = [_relative_directory(project, item) for item in includes]
    tests = [_relative_directory(project, item) for item in paths]
    for target in [*packages, *tests]:
        if not any(target == item or target.startswith(item + "/") for item in selected):
            raise MeizelError(f"{project}: strict typing does not include {target!r}")
    _check_source_includes(project, selected, files)
    _check_test_sources(project, tests, files)
    if _is_table(checker) and any(key in checker for key in ("exclude", "ignore")):
        raise MeizelError(f"{project}: standalone typing exclusions are unsupported")


def _ci_contract(project: str, text: str, packages: list[str], files: frozenset[str]) -> None:
    document = tomllib.loads(text)
    extras = _table(_table(document, "project"), "optional-dependencies")
    strict = _table(_table(_table(document, "tool"), "basedpyright"), "typeCheckingMode")
    valid = (
        project + "/uv.lock" in files
        and _is_table(extras)
        and _is_str_list(extras.get("cpu"))
        and strict == "strict"
    )
    if not valid:
        raise MeizelError(
            f"{project}: standalone CI ownership requires uv.lock, explicit cpu extra, "
            "strict basedpyright and pytest testpaths"
        )
    _check_coverage(project, document, packages, files)


def _project_directories(roots: tuple[str, ...], texts: Mapping[str, str]) -> list[str]:
    projects = sorted(path.removesuffix("/pyproject.toml") for path in texts)
    for root in roots:
        if not any(project == root or project.startswith(root + "/") for project in projects):
            raise MeizelError(f"{root}: standalone source has no explicit project metadata")
    for project in projects:
        if any(project.startswith(other + "/") for other in projects if other != project):
            raise MeizelError(f"{project}: nested standalone project ownership is ambiguous")
    return projects


def _owned_packages(
    project: str, packages: list[str], files: frozenset[str], aliases: dict[str, str]
) -> tuple[tuple[str, str], ...]:
    top_names = {path.split("/", 1)[0].removesuffix(".py") for path in files}
    owned: list[tuple[str, str]] = []
    for package in packages:
        name, physical = _package_path(project, package, files)
        if name in top_names or name in aliases:
            raise MeizelError(
                f"{project}: native package alias {name!r} collides with another owner"
            )
        aliases[name] = physical
        owned.append((name, physical))
    return tuple(owned)


def _check_orphans(files: frozenset[str], roots: tuple[str, ...], projects: list[str]) -> None:
    for path in files:
        if (
            path.endswith(".py")
            and any(path.startswith(root + "/") for root in roots)
            and not any(path.startswith(project + "/") for project in projects)
        ):
            raise MeizelError(f"{path}: standalone source has no owning project")


def standalone_projects(
    files: frozenset[str], root_manifest: str, texts: Mapping[str, str]
) -> tuple[StandaloneProject, ...]:
    roots = _standalone_roots(root_manifest)
    projects = _project_directories(roots, texts)
    _check_orphans(files, roots, projects)
    aliases: dict[str, str] = {}
    found: list[StandaloneProject] = []
    for project in projects:
        text = texts[project + "/pyproject.toml"]
        packages = _packages(project + "/pyproject.toml", text)
        owned = _owned_packages(project, packages, files, aliases)
        _ci_contract(project, text, packages, files)
        found.append(StandaloneProject(project, owned))
    return tuple(found)
