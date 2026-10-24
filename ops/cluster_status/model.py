# meiva:note
# Pure domain classification and session-grouping helpers for cluster status.

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import assert_never

from base.api_contracts.status import SchemaMismatchKind
from ops.rpc_schemas import AgentSessionGroup, SessionInfo

_AGENT_SESSION_RE = re.compile(r"-agent-(\d+)")
AGENT_SHELL_RE = re.compile(r"-agent-(\d+)-shell-(\d+)(?:-(.+))?$")


@dataclass(frozen=True)
class Mismatch:
    kind: SchemaMismatchKind
    detail: str


# meiva:note
# The three ways the schema evidence can fail to be read at all, as opposed to read and found
# different: the database's applied-migration catalog is malformed, this image's migration
# directory is malformed, or the database could not be queried.
class SchemaReadFailure(Enum):
    DB_CATALOG_INVALID = "db-catalog-invalid"
    IMAGE_LAYOUT_INVALID = "image-layout-invalid"
    DB_UNAVAILABLE = "db-unavailable"


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
# The diagnosis for evidence that could not be read: the error text for a malformed layout,
# the exception type name alone for an unreachable database (its message can carry a DSN).
def read_failure_mismatch(failure: SchemaReadFailure, error: str) -> Mismatch:
    match failure:
        case SchemaReadFailure.DB_CATALOG_INVALID:
            return Mismatch("invalid-migration-layout", f"DB migration catalog is invalid: {error}")
        case SchemaReadFailure.IMAGE_LAYOUT_INVALID:
            return Mismatch(
                "invalid-migration-layout", f"image migration layout is invalid: {error}"
            )
        case SchemaReadFailure.DB_UNAVAILABLE:
            return Mismatch("unavailable", f"applied migration set unavailable ({error})")
        case _:
            assert_never(failure)


# meiva:note
# Count agent shell and watcher sessions from the session list.
def count_agent_shells(sessions: Sequence[SessionInfo]) -> int:
    return sum(1 for s in sessions if re.search(r"-agent-(\d+)-shell-", s.name))


# meiva:note
# Group cluster sessions by agent ID into a structured list for the frontend.
def group_agent_sessions(sessions: Sequence[SessionInfo]) -> list[AgentSessionGroup]:
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
#
# A cursor-addressed TUI's pyte scrollback is dominated by blank rows (each full-screen redraw
# scrolls cleared rows into the history), so a plain tail can open with dozens of empty
# lines; trailing blank screen rows below the last line of a short session did the mirror-image
# damage. Blank padding at the extremes is never meaningful output; interleaved blanks are
# preserved.
def trim_captured_lines(captured: str) -> list[str]:
    captured_lines = captured.splitlines()
    while captured_lines and not captured_lines[0].strip():
        captured_lines.pop(0)
    while captured_lines and not captured_lines[-1].strip():
        captured_lines.pop()
    return captured_lines
