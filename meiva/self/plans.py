from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "SqlStatement",
    "compact_statement",
    "heartbeat_pause_statements",
    "pause_duration",
    "restart_statements",
    "terminate_statement",
]

_UPDATE_OVERLAY = (
    "UPDATE agents_meta "
    "SET config_overlay = COALESCE(config_overlay, '{}'::jsonb) || %s::jsonb "
    "WHERE id = %s"
)
_INSERT_RESTART = (
    "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
    "VALUES (%s, '', 'restart', 'self', %s::jsonb)"
)
_INSERT_TERMINATE = (
    "INSERT INTO inbound_messages (agent_id, content, kind, source) "
    "VALUES (%s, '', 'terminate', 'self')"
)
_INSERT_COMPACT = (
    "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, %s, 'compact_summary')"
)
_INSERT_PAUSE_LOG = "INSERT INTO heartbeat_pause_log (agent_id, duration_s) VALUES (%s, %s)"
_UPDATE_PAUSED_UNTIL = (
    "UPDATE agents_meta "
    "SET heartbeat_paused_until = now() + make_interval(secs => %s) "
    "WHERE id = %s"
)


# meiva:note
# One statement the `meiva.self` shell runs on its cursor. The lifecycle entries decide what
# to write here, as values; the shell owns the transaction, the cursor and the wake.
# meiva:link meiva/self/docs/self.meiva.okf.md
@dataclass(frozen=True, slots=True)
class SqlStatement:
    sql: str
    params: tuple[object, ...]


# meiva:note
# The rows a self-restart writes, in order: the overlay merge when one was given, then the
# restart inbound carrying it. The overlay arrives already validated and normalised by the
# shell, which checks the final stored pair under the row lock before running these.
# meiva:link meiva/self/docs/self.meiva.okf.md
def restart_statements(
    agent_id: int, config_overlay: Mapping[str, object] | None
) -> tuple[SqlStatement, ...]:
    if not config_overlay:
        return (SqlStatement(_INSERT_RESTART, (agent_id, None)),)
    overlay = dict(config_overlay)
    return (
        SqlStatement(_UPDATE_OVERLAY, (json.dumps(overlay), agent_id)),
        SqlStatement(
            _INSERT_RESTART,
            (agent_id, json.dumps({"config_overlay": overlay}, sort_keys=True)),
        ),
    )


def terminate_statement(target_id: int) -> SqlStatement:
    return SqlStatement(_INSERT_TERMINATE, (target_id,))


def compact_statement(agent_id: int, summary: str) -> SqlStatement:
    return SqlStatement(_INSERT_COMPACT, (agent_id, summary))


# meiva:note
# A heartbeat pause must be a positive number of seconds no larger than the configured
# limit; the limit is the shell's reading of the agent's setting, passed in as a value.
# meiva:link meiva/self/docs/self.meiva.okf.md
def pause_duration(duration: float, limit: float) -> float:
    if not duration > 0:
        raise ValueError(f"duration must be greater than 0 seconds, got {duration!r}")
    if duration > limit:
        raise ValueError(
            f"duration must be at most {limit:.0f} seconds (heartbeat pause limit; "
            "per-agent override via meiva.self.restart("
            "config_overlay={'heartbeat_pause_max_seconds': ...}), cluster default via "
            "MEIVA_HEARTBEAT_PAUSE_MAX_SECONDS), "
            f"got {duration!r}"
        )
    return float(duration)


def heartbeat_pause_statements(agent_id: int, seconds: float) -> tuple[SqlStatement, ...]:
    return (
        SqlStatement(_INSERT_PAUSE_LOG, (agent_id, seconds)),
        SqlStatement(_UPDATE_PAUSED_UNTIL, (seconds, agent_id)),
    )
