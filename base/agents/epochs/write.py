# meiva:link base/agents/epochs/docs/epochs.md

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import LiteralString

from base.agents.epochs.model import RecordExtent, SwitchedBy

Statement = tuple[LiteralString, tuple[object, ...]]

# meiva:link base/agents/epochs/docs/epochs.md
REASON_EXCERPT_CHARS = 500

_CLOSE_CURRENT: LiteralString = (
    "UPDATE agent_harness_epochs SET ended_at = clock_timestamp(), source_end = %s::bigint, "
    "session_id = COALESCE(session_id, %s::text) WHERE agent_id = %s AND ended_at IS NULL"
)
_OPEN_NEXT: LiteralString = (
    "INSERT INTO agent_harness_epochs "
    "(agent_id, seq, harness, model, session_id, source_start, switched_by, reason) "
    "SELECT %s::bigint, COALESCE(max(seq) + 1, 0), %s::text, %s::text, %s::text, %s::bigint, "
    "%s::text, %s::text FROM agent_harness_epochs WHERE agent_id = %s::bigint"
)
_STORE_HARNESS: LiteralString = (
    "UPDATE agents_meta SET harness = %s, harness_configured_at = clock_timestamp() WHERE id = %s"
)
_MARK_HARNESS_MODEL_SELECTION: LiteralString = (
    "UPDATE agents_meta SET birth_config = COALESCE(birth_config, '{}'::jsonb) "
    "|| '{\"harness_model_selection\": 1}'::jsonb WHERE id = %s"
)
_MERGE_OVERLAY: LiteralString = (
    "UPDATE agents_meta SET config_overlay = COALESCE(config_overlay, '{}'::jsonb) || %s::jsonb "
    "WHERE id = %s"
)


@dataclass(frozen=True, slots=True)
class SwitchFacts:
    new_harness: str
    model: str | None
    by: SwitchedBy
    message: str | None
    leaving: RecordExtent | None
    arriving: RecordExtent | None


def switched_by_of(source: str, agent_id: int) -> SwitchedBy:
    if source in ("self", f"agent:{agent_id}"):
        return "self"
    if source == "user":
        return "user"
    return "operator"


def reason_excerpt(message: str | None) -> str | None:
    text = (message or "").strip()
    return text[:REASON_EXCERPT_CHARS] or None


def merge_overlay(agent_id: int, overlay: Mapping[str, object]) -> Statement:
    return _MERGE_OVERLAY, (json.dumps(dict(overlay)), agent_id)


def birth_epoch(agent_id: int, *, harness: str, model: str | None) -> Statement:
    return _OPEN_NEXT, (agent_id, harness, model, None, None, "birth", None, agent_id)


def harness_switch(agent_id: int, facts: SwitchFacts) -> tuple[Statement, ...]:
    leaving, arriving = facts.leaving, facts.arriving
    statements: list[Statement] = [
        (
            _CLOSE_CURRENT,
            (
                leaving.offset if leaving else None,
                leaving.session_id if leaving else None,
                agent_id,
            ),
        ),
        (
            _OPEN_NEXT,
            (
                agent_id,
                facts.new_harness,
                facts.model,
                arriving.session_id if arriving else None,
                arriving.offset if arriving else None,
                facts.by,
                reason_excerpt(facts.message),
                agent_id,
            ),
        ),
        (_STORE_HARNESS, (facts.new_harness, agent_id)),
    ]
    if facts.new_harness != "meiva":
        statements.append((_MARK_HARNESS_MODEL_SELECTION, (agent_id,)))
    return tuple(statements)
