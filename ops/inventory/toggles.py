# meiva:note
# What a plugin / MCP enable request is allowed to change, as values.
#
# A request is all-or-nothing: every toggle is judged first (a plugin must be installed on this
# host; an MCP server must be known, and enabling one needs the host's capability verdict for its
# `requires` preconditions; disabling is always fine) and nothing is written unless every toggle
# passes. The shell reads the installed plugins, the merged MCP map and the host capability, and
# applies the toggles only when `applied` holds. Plugins and MCP servers are an agent-runner
# concern, so the same module also names the one precondition the ops share.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

__all__ = [
    "ToggleVerdicts",
    "Verdict",
    "mcp_summary",
    "plan_toggles",
    "require_agent_runner",
    "servers_to_probe",
]


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# The inventory ops run only on an agent-runner: a gateway runs no agent, so its inventory is
# whatever built-in plugins and MCP servers its checkout happens to carry, never actually
# loaded. Reaching an op on a gateway is a broken call path, not a degraded mode, so it fails
# loudly instead of surfacing the gateway's built-in items as if they were installed.
def require_agent_runner(roles: frozenset[str]) -> None:
    if "agent-runner" not in roles:
        raise RuntimeError(f"inventory ops run only on an agent-runner, not {sorted(roles)!r}")


# meiva:note
# One-line human summary of an MCP server entry: its url, or its command with its arguments.
#
# A local copy of the cli list-view helper, because `gateway` must not import `cli`. A value that
# is not a mapping is reported as malformed instead of failing the whole listing.
def mcp_summary(spec: object) -> str:
    if not _is_str_dict(spec):
        return "(malformed entry)"
    if "url" in spec:
        return str(spec["url"])
    command = spec.get("command", "")
    args = spec.get("args", [])
    parts = [str(command), *(str(arg) for arg in args)] if _is_object_list(args) else [str(command)]
    return " ".join(part for part in parts if part)


# meiva:note
# Whether one toggle may be applied, and why not when it may not.
@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None


# meiva:note
# The verdict of every requested toggle, in request order, and whether all of them passed (an
# empty request is trivially applied).
@dataclass(frozen=True)
class ToggleVerdicts:
    plugins: tuple[tuple[str, Verdict], ...]
    mcp_servers: tuple[tuple[str, Verdict], ...]
    applied: bool


# meiva:note
# The requested MCP servers whose capability verdict the plan needs: the known ones being enabled.
def servers_to_probe(mcp_servers: Mapping[str, bool], known: frozenset[str]) -> tuple[str, ...]:
    return tuple(name for name, enable in mcp_servers.items() if enable and name in known)


# meiva:note
# Judge every requested toggle against what this host has.
#
# `capabilities` holds the verdict for each name `servers_to_probe` returned; a missing one is a
# caller bug and raises instead of defaulting to "ok".
def plan_toggles(
    plugins: Mapping[str, bool],
    mcp_servers: Mapping[str, bool],
    *,
    installed_plugins: frozenset[str],
    known_servers: frozenset[str],
    capabilities: Mapping[str, Verdict],
) -> ToggleVerdicts:
    plugin_verdicts = tuple(
        (
            name,
            Verdict(ok=True, reason=None)
            if name in installed_plugins
            else Verdict(ok=False, reason="not installed on this host"),
        )
        for name in plugins
    )
    server_verdicts: list[tuple[str, Verdict]] = []
    for name, enable in mcp_servers.items():
        if name not in known_servers:
            verdict = Verdict(ok=False, reason="not a known MCP server on this host")
        elif not enable:
            verdict = Verdict(ok=True, reason=None)
        elif name in capabilities:
            verdict = capabilities[name]
        else:
            raise ValueError(f"no capability verdict for MCP server {name!r}")
        server_verdicts.append((name, verdict))
    applied = all(verdict.ok for _name, verdict in plugin_verdicts) and all(
        verdict.ok for _name, verdict in server_verdicts
    )
    return ToggleVerdicts(plugin_verdicts, tuple(server_verdicts), applied)
