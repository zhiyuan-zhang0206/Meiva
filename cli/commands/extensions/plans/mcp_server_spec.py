# meiva:note
# What `meiva mcp add` and `meiva mcp install` decide about one server entry, from values.
#
# The MCP config shape every vendor reads is `{"command": ..., "args": [...], "env": {...}}`.
# This module builds that entry from a pasted JSON object or from command-line flags, parses the
# repeated `--env KEY=VALUE` flags, names a server from a package's `pyproject.toml` name and
# summarizes an entry for the list view. It reads no file and prints nothing; the shell reads the
# config, writes it back and reports.

from __future__ import annotations

import json
from typing import TypeGuard

__all__ = ["build_server_spec", "derive_server_name", "parse_env_pairs", "server_summary"]

_NAME_PREFIXES = ("meiva-mcp-", "mcp-server-", "mcp-")


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# Parse repeated `KEY=VALUE` flags into an env dict. Raises `ValueError` for a pair without `=`.
def parse_env_pairs(env_pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in env_pairs:
        if "=" not in pair:
            raise ValueError(f"--env expects KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        env[key] = value
    return env


# meiva:note
# Build one server entry from either a pasted JSON object or `--command` flags.
#
# Raises `ValueError` when neither or both input forms are given, the JSON is malformed
# (`json.JSONDecodeError` is a `ValueError`), a `--env` pair lacks `=`, or the result is not a
# server object.
def build_server_spec(
    json_spec: str | None, command: str | None, args: list[str], env_pairs: list[str]
) -> dict[str, object]:
    if json_spec is not None and command is not None:
        raise ValueError("pass either --json or --command, not both")
    if json_spec is not None:
        parsed: object = json.loads(json_spec)
        if not _is_object_dict(parsed):
            raise ValueError(f"--json must be a server object (got {type(parsed).__name__})")
        return parsed
    if command is not None:
        spec: dict[str, object] = {"command": command}
        if args:
            spec["args"] = args
        env = parse_env_pairs(env_pairs)
        if env:
            spec["env"] = env
        return spec
    raise ValueError("need --json '<server object>' or --command CMD")


# meiva:note
# One-line description of a server entry for the list view: its url, else its command and args.
def server_summary(spec: object) -> str:
    if not _is_object_dict(spec):
        return "(malformed entry)"
    if "url" in spec:
        return str(spec["url"])
    command = spec.get("command", "")
    args = spec.get("args", [])
    parts = [str(command), *(str(a) for a in args)] if _is_object_list(args) else [str(command)]
    return " ".join(p for p in parts if p)


# meiva:note
# A clean server name from a pyproject ``name`` field: the first common prefix (``meiva-mcp-``,
# ``mcp-server-``, ``mcp-``) is stripped and underscores become hyphens, so the name is
# consistent with built-in and machine server naming.
def derive_server_name(raw_name: str) -> str:
    for prefix in _NAME_PREFIXES:
        if raw_name.lower().startswith(prefix):
            raw_name = raw_name[len(prefix) :]
            break
    return raw_name.replace("_", "-").strip("-")
