# meiva:note
# How the daemon reaches one configured MCP server, decided from its `.mcp.json` entry. `plan_connect` answers with the transport (a remote endpoint, a stdio child, or a direct dial of a per-machine service) and `unmet_requirement` says why a host cannot run the server at all; the daemon shell connects what the plan says and raises what the requirement says.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast


# meiva:note
# A remote Streamable HTTP endpoint: static `headers` (API-key auth) or `oauth` (browser authorization flow), never both.
@dataclass(frozen=True)
class HttpConnect:
    server: str
    url: str
    headers: Mapping[str, str] | None
    oauth: bool


# meiva:note
# A stdio child the daemon spawns; `env` is None when the entry declares none.
@dataclass(frozen=True)
class StdioConnect:
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str] | None


# meiva:note
# No child at all: the daemon dials the line protocol of the per-machine browser-mcp or computer-mcp service.
@dataclass(frozen=True)
class DirectConnect:
    service: Literal["browser", "computer_use"]


type ConnectPlan = HttpConnect | StdioConnect | DirectConnect


# meiva:note
# The refusal for a server whose `requires` the host cannot meet, or None when it can run here. `requires` is the validated `requires` map of the entry (`validate_requirements`).
def unmet_requirement(requires: Mapping[str, bool], *, display_available: bool) -> str | None:
    if requires.get("display") and not display_available:
        return "MCP server requires a display, but this host has none (headless server)"
    return None


def _strings(server: str, field_name: str, value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in cast(Sequence[object], value)
    ):
        raise TypeError(f"Server {server!r}: {field_name} must be a list of strings, got {value!r}")
    return list(cast(Sequence[str], value))


def _string_map(server: str, field_name: str, value: object) -> dict[str, str]:
    items = cast(Mapping[object, object], value).items() if isinstance(value, dict) else None
    if items is None or not all(isinstance(k, str) and isinstance(v, str) for k, v in items):
        raise TypeError(
            f"Server {server!r}: {field_name} must map strings to strings, got {value!r}"
        )
    return cast(dict[str, str], value)


# meiva:note
# `url` is the entry's validated endpoint (`server_url`), None for a command entry. An unknown `shared` value raises: silently falling back to a per-connection child would defeat the declared intent. A malformed `args` or `env` raises instead of reaching the child.
def plan_connect(server: str, spec: Mapping[str, object], *, url: str | None) -> ConnectPlan:
    if url is not None:
        headers = spec.get("headers")
        return HttpConnect(
            server=server,
            url=url,
            headers=None if headers is None else _string_map(server, "headers", headers),
            oauth=bool(spec.get("oauth")),
        )
    shared = spec.get("shared")
    if shared == "browser":
        return DirectConnect("browser")
    if shared == "computer_use":
        return DirectConnect("computer_use")
    if shared not in (None, False, True):
        raise ValueError(
            f"Server {server!r}: unknown shared value {shared!r} "
            f'(expected true, "browser", or "computer_use")'
        )
    command = spec["command"]
    if not isinstance(command, str):
        raise TypeError(f"Server {server!r}: command must be a string, got {command!r}")
    env = _string_map(server, "env", spec.get("env") or {})
    return StdioConnect(
        command=command,
        args=tuple(_strings(server, "args", spec.get("args") or [])),
        env=env or None,
    )
