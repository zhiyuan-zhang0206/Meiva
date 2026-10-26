# meiva:note
# The environment variables that carry an agent's id, config overlay and birth stamp to a child.
#
# The config ones travel as JSON in env vars, never argv (issue #974: argv is world-readable via `ps`). They
# are not Settings fields: they are process-bound carriers the exec launcher writes and the child
# boot consumes.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

__all__ = [
    "AGENT_BIRTH_CONFIG_ENV",
    "AGENT_CONFIG_OVERLAY_ENV",
    "AGENT_ID_ENV",
    "ModelOverrides",
]

AGENT_ID_ENV = "MEIVA_AGENT_ID"
AGENT_CONFIG_OVERLAY_ENV = "MEIVA_AGENT_CONFIG_OVERLAY"
AGENT_BIRTH_CONFIG_ENV = "MEIVA_AGENT_BIRTH_CONFIG"


@dataclass(frozen=True)
class ModelOverrides:
    llm_stream_ttft_timeout_seconds: float | None
    llm_stream_total_timeout_seconds: float | None
    llm_stream_inter_chunk_timeout_seconds: float | None
    reasoning_effort: str | None
    claude_thinking_budget_tokens: int | None
    auto_compact_fraction: float | None
    auto_compact_ceiling_tokens: int | None
    compact_reminder_fraction: float | None

    @classmethod
    def from_pins(cls, pins: Mapping[str, object] | None) -> ModelOverrides:
        raw: Mapping[str, Any] = pins or {}
        return cls(**{f.name: raw.get(f.name) for f in fields(cls)})
