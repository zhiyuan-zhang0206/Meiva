from __future__ import annotations

from langchain_core.messages import AnyMessage
from pydantic import BaseModel, Field

from base.agents.context.external_attachment.nodes import CLAIM, NodeName


# meiva:note
# Compaction bookkeeping nested under `BaseAgentState.compact`. `version` increments
# monotonically on each compaction and never resets; `understanding_cut_index` and
# `understanding_cut_tokens` reset to 0 with each segment via `next_segment`.
# Last-value channel: writers `model_copy(update=...)` only the fields they change.
# meiva:link agent/docs/state.meiva.okf.md
class CompactState(BaseModel):
    version: int = 0
    reminder_shown: bool = False
    reminder_seen_version: int = 0
    understanding_cut_index: int = 0
    understanding_cut_tokens: int = 0

    def next_segment(self) -> CompactState:
        return self.model_copy(
            update={
                "version": self.version + 1,
                "understanding_cut_index": 0,
                "understanding_cut_tokens": 0,
            }
        )


# meiva:link agent/docs/state.meiva.okf.md
class AttachEntry(BaseModel):
    path: str
    label: str | None


def _new_pending_attachments() -> list[AttachEntry]:
    return []


# meiva:note
# Files registered during a turn, drained into a media message by the exec node at
# the end of the registering turn (with claim-boundary drain as fallback).
# meiva:link agent/docs/state.meiva.okf.md
class AttachState(BaseModel):
    pending: list[AttachEntry] = Field(default_factory=_new_pending_attachments)


# meiva:note
# Passive-recall bookkeeping nested under `BaseAgentState.memory`. `injected_paths`
# accumulates surfaced note paths within one compaction `window` so notes are not
# re-injected in the same context window.
# meiva:link agent/docs/state.meiva.okf.md
class MemoryState(BaseModel):
    injected_paths: set[str] = Field(default_factory=set)
    window: int = 0


# meiva:note
# Unions `injected_paths` within one compaction window and replaces the record when
# `new.window > old.window`.
# meiva:link agent/docs/state.meiva.okf.md
def memory_state_merge(old: MemoryState, new: MemoryState) -> MemoryState:
    if new.window > old.window:
        return new
    return MemoryState(injected_paths=old.injected_paths | new.injected_paths, window=old.window)


# meiva:note
# Skill display identifiers listed in the standing `# Capabilities` SystemMessage.
# `None` on pre-existing checkpoints causes the drift check to adopt the live
# catalog as baseline without announcing the entire catalog as newly installed.
# meiva:link agent/graph/docs/system-prompt/capability-index-freshness.meiva.okf.md
class CapabilitiesState(BaseModel):
    indexed: set[str] | None = None


# meiva:note
# Skill identifiers offered by recall notes in the current `compact.version` window;
# cleared alongside `BaseAgentState.skill_reads` when `window` advances.
# meiva:link agent/graph/docs/skill-recall.meiva.okf.md
class SkillRecallState(BaseModel):
    prompted: set[str] = Field(default_factory=set)
    window: int = 0


def _new_any_messages() -> list[AnyMessage]:
    return []


# meiva:note
# Pending context (re)establishment consumed and cleared by `init_context`, which
# lays down the standing head, appends `tail`, and routes to `resume`.
# meiva:link agent/docs/state.meiva.okf.md
class ContextReset(BaseModel):
    tail: list[AnyMessage] = Field(default_factory=_new_any_messages)
    resume: NodeName = CLAIM


CIRCUIT_REASON_CONTEXT_OVERFLOW = "context_overflow"
CIRCUIT_REASON_AUTH = "auth"
CIRCUIT_REASON_BILLING = "billing"
CIRCUIT_REASON_FORBIDDEN = "forbidden"
CIRCUIT_REASON_MODEL_NOT_FOUND = "model_not_found"
CIRCUIT_REASON_SCHEMA = "schema"
CIRCUIT_REASON_BAD_REQUEST = "bad_request"
CIRCUIT_REASON_PERMANENT = "permanent"


# meiva:note
# Suppresses background model calls after a permanent provider rejection while
# allowing fresh chats and explicit controls through normal claim routing.
# meiva:link agent/docs/state.meiva.okf.md
class CircuitState(BaseModel):
    open: bool = False
    reason: str | None = None
    opened_at: str | None = None

    @property
    def parks_idle(self) -> bool:
        return self.open


# meiva:note
# Preserves `("agent.state_channels", "<Name>")` msgpack ext identities across
# existing checkpoints and `STATIC_CHECKPOINT_MSGPACK_TYPES`.
# meiva:link agent/docs/state.meiva.okf.md
for _cls in (
    AttachEntry,
    AttachState,
    CapabilitiesState,
    CircuitState,
    CompactState,
    ContextReset,
    MemoryState,
    SkillRecallState,
):
    _cls.__module__ = "agent.state_channels"


__all__ = [
    "CIRCUIT_REASON_AUTH",
    "CIRCUIT_REASON_BAD_REQUEST",
    "CIRCUIT_REASON_BILLING",
    "CIRCUIT_REASON_CONTEXT_OVERFLOW",
    "CIRCUIT_REASON_FORBIDDEN",
    "CIRCUIT_REASON_MODEL_NOT_FOUND",
    "CIRCUIT_REASON_PERMANENT",
    "CIRCUIT_REASON_SCHEMA",
    "AttachEntry",
    "AttachState",
    "CapabilitiesState",
    "CircuitState",
    "CompactState",
    "ContextReset",
    "MemoryState",
    "SkillRecallState",
    "memory_state_merge",
]
