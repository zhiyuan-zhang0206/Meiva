"""The TimelineItem model — one element of the rendered timeline view.

Extracted from `base/agents/history/timeline.py` (file line budget; task #3323): the model
contains the wire fields and their metadata models. The projection logic
that builds the items stays in `base/agents/history/timeline.py`, which re-exports this class so existing
importers keep working.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from base.agents.messages.inbound_images import TimelineUploadFile
from base.agents.sdk.tally import SdkCall


class TimelineItem(BaseModel):
    """One element of a timeline view of LangGraph state.messages, using the
    inbound_messages table as a timestamp anchor and impersonation history
    expanded at its checkpoint marker. External session metadata identifies
    the actual executor without changing the cursor grammar.

    `item_id` is the stable key coordinating frontend timeline with
    streaming SSE; the current segment uses `f"{msg_idx}.{block_idx}"`
    (msg_idx = position in state.messages; for other message types
    block_idx=0). Cold-loaded compact history prefixes that local position
    with `s<rank>.<boundary_checkpoint_id>.`; it never enters the live SSE
    merge path. For an
    AIMessage, text/thinking are content blocks (block_idx = their content
    position) and each tool call gets `block_idx = <number of text/thinking
    content blocks> + <ordinal in msg.tool_calls>` — a provider-agnostic
    rule derived from langchain's normalized `tool_calls` view, so it holds
    whether the provider carries tool calls inside `content` (anthropic
    `tool_use` blocks) or only in the separate `tool_calls` list
    (gemini / openai). During streaming, the frontend uses `*_start`
    event's `item_id` to create items; after commit, the same rule
    recomputes id, and matching ids on both sides means the same logical
    item. On merge, the snapshot version wins, avoiding race bugs from
    ts-heuristic matching.
    """

    item_id: str
    activity_revision: int | None = Field(default=None, ge=0, strict=True)
    activity_pending: bool | None = None
    kind: Literal[
        "inbound_chat",
        "inbound_compact_summary",
        "inbound_compact_request",
        "attach",
        "agent_chat",
        "agent_code",
        "agent_reasoning",
        "code_output",
        "subagent_text",
        "system_prompt",
        "system_marker",
    ]
    source: str | None = None
    # meiva:note
    # A CLI sub-agent's activity (`subagent_text`, and the `agent_code` / `code_output` pair of a tool
    # call the sub-agent made): the `agent_code` item of the call that started the sub-agent, when it
    # can be found (the same invocation, or an earlier one through `tool_use_id`). None for the main
    # agent's own items. On `subagent_text`, `source` carries the CLI's own label for the sub-agent.
    # meiva:link base/lm/harness/docs/subagent-text.md
    parent_item_id: str | None = None
    # meiva:note
    # `agent_code` of an external harness only: the CLI's id of the tool call. It is what ties a
    # sub-agent's activity to the call that started it when the activity arrives after the graph
    # invocation that wrote the call has ended.
    # meiva:link base/lm/harness/docs/subagent-text.md
    tool_use_id: str | None = None
    payload: str  # rendered content
    created_at: str | None = None  # ISO-8601
    inbound_id: int | None = None
    # Durable source coordinates; item_id remains the frontend position key.
    # Missing/ephemeral source IDs are excluded. Only an explicitly embedded
    # inbound row ID qualifies.
    source_message_id: str | None = None
    source_inbound_id: int | None = None
    source_block_idx: int | None = None
    # The compact run this item belongs to (None elsewhere): a forced/auto
    # compact's summary message carries `meiva_compact_id`, pairing the item
    # with the live compact_started / compact_finished events (ticking
    # "Compacting" block → summary item handoff on the frontend).
    compact_id: str | None = None
    # Reasoning-block summary on `agent_reasoning` items (None elsewhere).
    # Drives the collapsed-reasoning chip ("Thought for 8s / 1.2k tokens") so
    # the user keeps a sense of the thinking without reading it.
    # `reasoning_ms` is per-block: the real wall-clock the llm node measured
    # for that thinking block (agent/graph/_callbacks.py) and persisted on the
    # message — a turn with several thinking blocks carries one value per
    # block. `reasoning_tokens` stays turn-level (usage_metadata reports one
    # total) and sits on the first thinking item only.
    reasoning_ms: int | None = None
    reasoning_tokens: int | None = None
    # Wall-clock the code ran, set only on `code_output` items (None elsewhere).
    # Read from the exec_output message's meiva_exec_ms (agent/graph/exec/node.py).
    # Drives the collapsed-output chip ("ran in 1.3s").
    exec_ms: int | None = None
    # Whether the frontend chip shows this item's wall-clock ts. True everywhere
    # except system_marker notes that read as standing context rather than
    # events (memory recall + the one-time guidance notes) — see
    # `_NO_TIMESTAMP_NOTE_TAGS`. Default True keeps every other kind unchanged.
    show_timestamp: bool = True
    # meiva:note
    # Whether an `agent_chat` item surfaces as a primary user-facing reply in the main dialog
    # stream and reply suggestions. False demotes unprefixed closing/narration text
    # from non-human background wakes into the collapsible `Details` work block while keeping it
    # inspectable and copyable.
    # meiva:link base/agents/history/docs/timeline/timeline.md
    user_facing: bool = True
    # Image urls on a multimodal `inbound_chat` / `attach` item (None
    # elsewhere): inbound_chat = gateway-relative urls from meiva_image_urls
    # (via assetUrl); attach = data URIs from content blocks (rendered raw).
    images: list[str] | None = None
    files: list[TimelineUploadFile] | None = None
    # Per-image caption lines on an `attach` item, aligned 1:1 with `images`:
    # the backend-generated "- [N] name (mime, size) — \"label\"" line that
    # precedes each image block in the message content, so the frontend can
    # interleave every thumbnail with its own label. None on other kinds and on
    # legacy attach messages whose caption was a single text block (no pairing
    # information survives there).
    image_captions: list[str] | None = None
    # SDK calls the block really executed (the runtime tally, read from its
    # exec_output ToolMessage's `sdk_calls`; None until that lands / other kinds).
    sdk_calls: list[SdkCall] | None = None
    # Wall-clock the code-generation took, set only on `agent_code` items (None
    # elsewhere). Read from the AIMessage's meiva_code_ms_by_block (keyed by code
    # block_idx). Drives the detail-block chip "Wrote code for Xs".
    code_elapsed_ms: int | None = None
