"""Wire models of the agent surface: the selected-agent row, threads and
inbound messages, notices, and the per-agent token-usage / context-breakdown
reads.

FastAPI registers these unchanged, so the OpenAPI codegen is byte-identical to
the wire before.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from base.agents.messages.envelope import InboundSender, validate_source
from base.agents.messages.kwargs import NoteTag
from base.agents.observation.snapshot import AgentSnapshot
from base.agents.tasks.priority import Priority
from base.api_contracts.agent_contract import AgentStatus
from base.api_contracts.notice_question import NoticeAnswer, NoticeQuestion
from ops.rpc_schemas.content import UserContent


class AgentRow(AgentSnapshot):
    """GET /api/agents/{id} detail, including response-required notice bodies.

    The directory and live roster use bounded cards from base.agents.observation.roster.
    last_active_at is the real-activity clock; last_inbound_at is the latest
    inbound message clock.
    """


class LabelPatchRequest(BaseModel):
    """PATCH /api/agents/{id} body — manually set / reset agent label.

    `label=""` resets back to NULL; the frontend re-displays fallback
    `#N`. Non-empty strings are stripped; length 1-64 inclusive. `source` names who
    changed it in the `label_change` audit fact: the operator by default, `"self"` when
    the agent sets its own label.
    """

    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]
    source: Literal["user", "self"] = "user"


class AgentMessageEnqueued(BaseModel):
    """POST /messages receipt — durable inbound id plus current agent status.

    Same-key retries return the same `inbound_id`; `status` may be recomputed
    because auto-resurrection can advance while the client reconciles.
    """

    status: AgentStatus
    inbound_id: int | None = None


class SystemNoteIn(BaseModel):
    """POST /api/agents/{id}/system-note request body — a framework system
    notification delivered to the agent as a system note (system_marker in
    the timeline), never a peer chat message.

    `content` is the note text. `source` follows the same envelope whitelist
    as chat messages (system / agent:N / user / ...), defaulting to 'system';
    it is recorded for audit but the note itself renders without a sender
    prefix. `note_tag` picks the timeline chip — must be a NoteTag value
    (closed set); `task` is the task-notification family. `resurrect`
    controls whether a terminated target is woken to receive the note: task
    assignment (a delegator direction) resurrects, plain update notices do
    not (user ruling 2026-08-27 — notifications never resurrect an owner).
    `task_id` is optional but requires `note_tag='task'`; it is the explicit
    LLM-usage attribution for the turn this note drives.
    """

    content: str = Field(min_length=1)
    source: str = Field(default="system", min_length=1, max_length=64)
    note_tag: str = "task"
    task_id: int | None = Field(default=None, gt=0)
    resurrect: bool = True

    @field_validator("note_tag")
    @classmethod
    def _check_note_tag(cls, v: str) -> str:
        try:
            NoteTag(v)
        except ValueError as exc:
            raise ValueError(f"note_tag {v!r} is not a NoteTag value") from exc
        return v

    @field_validator("source")
    @classmethod
    def _check_envelope_source(cls, v: str) -> str:
        validate_source(v)
        return v

    @model_validator(mode="after")
    def _check_task_attribution(self) -> "SystemNoteIn":
        if self.task_id is not None and self.note_tag != NoteTag.TASK.value:
            raise ValueError("task_id requires note_tag='task'")
        return self


class ResolveNoticeIn(BaseModel):
    # meiva:link gateway/agents/docs/notice-receipts.meiva.okf.md

    model_config = ConfigDict(extra="forbid")
    action: Literal["answer", "dismiss", "read"]
    reply: UserContent | None = None
    answer: NoticeAnswer | None = None

    @model_validator(mode="after")
    def validate_answer_intent(self) -> "ResolveNoticeIn":
        if self.answer is not None and (self.action != "answer" or self.reply is not None):
            raise ValueError("structured answer requires action=answer and cannot accompany reply")
        return self


class NoticeCreateIn(BaseModel):
    # meiva:link meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md

    model_config = ConfigDict(extra="forbid")
    title: str
    content: str | None = None
    priority: Priority = Priority.P2
    require_response: Annotated[bool, Field(strict=True)] = True
    blocking: Annotated[bool, Field(strict=True)] = False
    task_id: int | None = None
    expire_at: datetime | None = None
    question: NoticeQuestion | None = None


class NoticeEditIn(BaseModel):
    # meiva:link meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md

    title: str | None = None
    content: str | None = None
    priority: Priority | None = None
    blocking: bool | None = None


class NoticeItem(BaseModel):
    # meiva:link meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md

    id: int
    agent_id: int
    agent_label: str | None
    title: str
    content: str | None
    priority: Priority
    require_response: bool
    blocking: bool
    created_at: datetime
    updated_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None
    reply: str | None = None
    question: NoticeQuestion | None = None
    answer: NoticeAnswer | None = None
    # The task this notice belongs to, or None — lets the feed group by task.
    task_id: int | None = None
    expire_at: datetime


class NoticesCursor(BaseModel):
    """Keyset cursor for the resolved-history page of GET /api/notices —
    a (resolved_at, id) pair. Supplied together or not at all;
    `before_at` is the last row's resolution time, `before_id` its id."""

    before_at: datetime
    before_id: int


class NoticesFeed(BaseModel):
    # meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md

    open: list[NoticeItem]
    awaiting: list[NoticeItem]
    resolved_page: list[NoticeItem]
    next_cursor: NoticesCursor | None


class PendingInbound(BaseModel):
    """One queued inbound (status='pending', kind='chat') — element of
    GET /api/agents/{id}/pending.

    These have not been claimed by the agent yet, so they are absent from
    the timeline snapshot; the web UI shows them as a compact strip above
    the composer. `source` disambiguates origin (user / agent:N /
    watcher:N / ...) so the UI can label who queued it.

    `images` carries the image reference urls of a multimodal inbound (None
    when the message has no image) — the same gateway-relative upload urls
    the timeline renders for a claimed message, so the strip shows
    thumbnails with one contract.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    source: str | None
    sender: InboundSender
    content: str
    images: list[str] | None
    created_at: datetime


class AgentMessagesResponse(BaseModel):
    """GET /api/agents/{id}/messages response — the raw, unrendered
    state.messages for programmatic consumers (ops scripts / other agents /
    evals). The rendered, frontend-facing view is GET .../timeline; this is
    the data layer beneath it.

    `messages[i]` is one LangChain BaseMessage `model_dump()` (raw fields:
    type / content / tool_calls / id / additional_kwargs / ...) and
    corresponds to `state.messages[start_index + i]`. `msg_count` is the
    total length of state.messages; `start_index` is the absolute index of
    the first returned message. `has_more` reports whether older messages
    exist; when true, `start_index` is the exclusive `before` cursor for the
    next older page.
    """

    messages: list[dict[str, Any]]
    msg_count: int
    start_index: int
    has_more: bool


class LastMessageResponse(BaseModel):
    """GET /api/agents/{id}/last-message response — the text of the last
    AI message, or None when no AI message with text content exists yet.
    """

    text: str | None


class TraceCheckpointMessagesResponse(BaseModel):
    """GET /api/agents/{id}/traces/{trace_id}/messages response — the full
    message list (incl. the system prompt) of the turn that produced one OTel
    trace, resolved on demand from the checkpoints table.

    Spans are metadata-only (trace v2): this endpoint is how content comes
    back. `pruned` distinguishes the two absence shapes:
    - pruned=true, checkpoint_id=None — no checkpoint carries this trace id
      (the link predates the feature, or the turn committed no checkpoint), so
      there is no content to return; the span metadata in the mirror still
      exists. Checkpoints are never deleted. Callers render this as absent
      content rather than an error.
    - pruned=false — the checkpoint exists; messages is its `messages` channel
      (the full conversation at that point, system prompt included).

    `messages[i]` is one LangChain BaseMessage `model_dump()` — the same raw
    shape as GET /api/agents/{id}/messages.
    """

    trace_id: str
    agent_id: int
    checkpoint_id: str | None
    pruned: bool
    messages: list[dict]


class TokenUsageResponse(BaseModel):
    """GET /api/agents/{id}/token-usage response — token usage of the
    most recent LLM call. `input_tokens` is context-window occupancy.

    Real-time deltas rely on the SSE `token_usage` event; when
    switching back to an existing agent, the frontend uses this endpoint
    to fetch the historical last value to initialize the UI (SSE is
    fire-and-forget with no persistence). Returns `(0, 0, 0)` if no
    AIMessage in state.messages carries usage_metadata (new agent /
    has not run any LLM call yet). `reasoning_tokens` is the
    reasoning/thinking portion of output (gemini/openai reasoning
    models + Anthropic thinking); defaults to 0 when absent.

    `soft_compact_tokens` / `hard_compact_tokens` are the agent model's
    wind-down-reminder and force-compact thresholds (a fraction of
    `max_input_tokens`, resolved per agent), so the composer can render the two
    marks against current occupancy without a second request. Both default to 0
    when the agent's model has no known context window.
    """

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    max_input_tokens: int = 0
    soft_compact_tokens: int = 0
    hard_compact_tokens: int = 0
