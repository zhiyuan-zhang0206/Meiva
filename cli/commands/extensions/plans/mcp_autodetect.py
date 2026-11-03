# meiva:note
# What `meiva mcp install` derives for a package that ships no `.mcp.json`: the server name and the
# Python module to run, from the package's `pyproject.toml` and its directory layout, and the
# `.mcp.json` written from them.

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TypeGuard

from pydantic import JsonValue

from base.host.boundary.untyped import object_of
from cli.commands.extensions.plans.mcp_server_spec import derive_server_name

__all__ = ["auto_detect_mcp", "write_mcp_json"]


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The first package with a `__main__.py`, in search order.
def _main_module_candidates(search_dirs: list[Path]) -> list[str]:
    for base in search_dirs:
        for child in sorted(base.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and child.name != "src"
                and (child / "__main__.py").is_file()
            ):
                return [child.name]
    return []


# meiva:note
# The Python package directories, in deterministic search order.
def _package_candidates(search_dirs: list[Path]) -> list[str]:
    packages: list[str] = []
    for base in search_dirs:
        for child in sorted(base.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and child.name != "src"
                and (child / "__init__.py").is_file()
            ):
                packages.append(child.name)
    return packages


# meiva:note
# The Python module to run via `-m` for a package tree; the first of these that applies wins:
# 1. a top-level directory (or `src/<dir>`) whose `__main__.py` exists, the most reliable signal of
#    a `python -m <module>` entry point;
# 2. the first `[project.scripts]` entry, the module part (left of `:`) of its `module:function`;
# 3. the single top-level Python package (a directory with `__init__.py`), when there is exactly one;
# 4. `fallback_name` with underscores instead of hyphens.
def _detect_module(pkg_dir: Path, project: Mapping[str, object], fallback_name: str) -> str:
    search_dirs = [pkg_dir]
    src_dir = pkg_dir / "src"
    if src_dir.is_dir():
        search_dirs.append(src_dir)
    main_modules = _main_module_candidates(search_dirs)
    if main_modules:
        return main_modules[0]
    scripts = project.get("scripts")
    if _is_object_dict(scripts) and scripts:
        first_value = next(iter(scripts.values()))
        if isinstance(first_value, str) and ":" in first_value:
            module = first_value.split(":", 1)[0].strip()
            if module:
                return module
    packages = _package_candidates(search_dirs)
    if len(packages) == 1:
        return packages[0]
    return fallback_name.replace("-", "_")


# meiva:note
# The MCP server name and spec of a package that lacks `.mcp.json`, derived from its
# `pyproject.toml`: the name, and the Python module to run. The spec carries `command`, `args`,
# `env` and `description`, the shape of one entry under an `.mcp.json` `mcpServers` key.
#
# Raises `ValueError` when `pyproject.toml` is missing, unparseable, or does not carry enough
# information to detect the server.
def auto_detect_mcp(pkg_dir: Path) -> tuple[str, dict[str, JsonValue]]:
    pyproject = pkg_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise ValueError(
            "no .mcp.json or pyproject.toml found; "
            "an MCP package must ship at least one of these at its root"
        )
    try:
        data = object_of(tomllib.loads, pyproject.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"cannot parse pyproject.toml: {e}") from e

    project = data.get("project") if _is_object_dict(data) else None
    if not _is_object_dict(project):
        raise ValueError("pyproject.toml [project] section is missing or not a table")
    raw_name = project.get("name", "")
    if not raw_name:
        raise ValueError("pyproject.toml [project] has no 'name' field")
    name = derive_server_name(str(raw_name))
    module = _detect_module(pkg_dir, project, name)
    description = str(project.get("description", "")) or f"{name} MCP server"

    spec: dict[str, JsonValue] = {
        "command": ".venv/bin/python",
        "args": ["-m", module],
        "env": {},
        "description": description,
    }
    return name, spec


# meiva:note
# Write a generated `.mcp.json` into `dest` for a single server.
def write_mcp_json(dest: Path, name: str, spec: dict[str, JsonValue]) -> None:
    data = {"mcpServers": {name: spec}}
    (dest / ".mcp.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
