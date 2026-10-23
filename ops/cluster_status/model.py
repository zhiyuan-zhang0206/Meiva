# meiva:note
# Pure domain classification and session-grouping helpers for cluster status.

from __future__ import annotations

import re
from dataclasses import dataclass

from base.api_contracts.status import SchemaMismatchKind
from ops.rpc_schemas import AgentSessionGroup, SessionInfo

_AGENT_SESSION_RE = re.compile(r"-agent-(\d+)")
AGENT_SHELL_RE = re.compile(r"-agent-(\d+)-shell-(\d+)(?:-(.+))?$")


@dataclass(frozen=True)
class Mismatch:
    kind: SchemaMismatchKind
    detail: str


# meiva:note
# Compare the current database with this running image, without a Git pin.
def classify(applied: set[str], required: set[str]) -> Mismatch | None:
    extra = applied - required
    missing = required - applied
    if extra and missing:
        return Mismatch(
            "divergent",
            f"DB has {len(extra)} migration(s) this image lacks and lacks "
            f"{len(missing)} required by this image",
        )
    if extra:
        return Mismatch(
            "schema-ahead-of-code", f"DB has {len(extra)} migration(s) this image lacks"
        )
    if missing:
        return Mismatch(
            "schema-behind-code", f"DB lacks {len(missing)} migration(s) required by this image"
        )
    return None


# meiva:note
# Count agent shell and watcher sessions from the session list.
def count_agent_shells(sessions: list[SessionInfo]) -> int:
    return sum(1 for s in sessions if re.search(r"-agent-(\d+)-shell-", s.name))


# meiva:note
# Group cluster sessions by agent ID into a structured list for the frontend.
def group_agent_sessions(sessions: list[SessionInfo]) -> list[AgentSessionGroup]:
    groups: dict[int, AgentSessionGroup] = {}
    for s in sessions:
        m = _AGENT_SESSION_RE.search(s.name)
        if m is None:
            continue
        agent_id = int(m.group(1))
        if agent_id not in groups:
            groups[agent_id] = AgentSessionGroup(agent_id=agent_id, label="", shells=[])
        groups[agent_id].shells.append(s)
    return list(groups.values())


# meiva:note
# Split terminal output into lines and trim blank padding at both extremes.
def trim_captured_lines(captured: str) -> list[str]:
    captured_lines = captured.splitlines()
    while captured_lines and not captured_lines[0].strip():
        captured_lines.pop(0)
    while captured_lines and not captured_lines[-1].strip():
        captured_lines.pop()
    return captured_lines
