from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.reply_intent import resolve_agent_reply
from base.agents.sdk.tally import SdkCall
from base.lm.harness.session.transcript.drafts import DraftKind, TimelineDraft

# meiva:note
# What one timeline draft of a harness record becomes as a `TimelineItem`: reply visibility
# (`resolve_agent_reply`, which lives above `base/lm`) and the rendering of a tool call are applied here, once,
# so the projection stays free of both. No item id is decided here: the caller numbers the items. A sub-agent's
# text and tool output are cut at `SUBAGENT_ITEM_CHARS`, as they are on the event path today.
# meiva:link base/lm/harness/docs/transcript-projection.md

SUBAGENT_ITEM_CHARS = 8_000
SUBAGENT_CUT = "\n[... sub-agent text cut]"


@dataclass(frozen=True, slots=True)
class ReplyPolicy:
    explicit_visibility: bool
    delivery_mode: str | None


def item_of(
    draft: TimelineDraft,
    sent_items: Mapping[str, TimelineItem],
    *,
    non_human: bool,
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
) -> TimelineItem:
    match draft.kind:
        case DraftKind.INBOUND_CHAT:
            return _inbound(draft, sent_items)
        case DraftKind.AGENT_CHAT:
            payload, user_facing = resolve_agent_reply(
                draft.payload,
                non_human_wake=non_human,
                explicit_visibility=policy.explicit_visibility,
                delivery_mode=policy.delivery_mode,
            )
            return _base(draft, "agent_chat", payload, user_facing=user_facing)
        case DraftKind.AGENT_REASONING:
            return _base(draft, "agent_reasoning", draft.payload, reasoning_ms=draft.reasoning_ms)
        case DraftKind.AGENT_CODE:
            name = draft.tool_name or ""
            return _base(
                draft,
                "agent_code",
                render_tool(name, draft.tool_input or {}),
                source=name if name != "execute_code" else None,
                tool_use_id=draft.tool_use_id,
                sdk_calls=[SdkCall(method=name, count=1)],
            )
        case DraftKind.CODE_OUTPUT:
            return _base(draft, "code_output", _cut(draft, draft.payload), exec_ms=draft.exec_ms)
        case DraftKind.SUBAGENT_TEXT:
            return _base(
                draft,
                "subagent_text",
                _cut(draft, draft.payload),
                source=draft.source,
                user_facing=False,
            )
        case DraftKind.SYSTEM_MARKER:
            return _base(draft, "system_marker", draft.payload, source=draft.source)


def _inbound(draft: TimelineDraft, sent_items: Mapping[str, TimelineItem]) -> TimelineItem:
    prebuilt = sent_items.get(draft.part_ref) if draft.part_ref else None
    if prebuilt is not None:
        return prebuilt
    return _base(draft, "inbound_chat", draft.payload, source=draft.source)


def _cut(draft: TimelineDraft, text: str) -> str:
    if draft.parent_tool_use_id is None or len(text) <= SUBAGENT_ITEM_CHARS:
        return text
    return text[:SUBAGENT_ITEM_CHARS] + SUBAGENT_CUT


def _base(draft: TimelineDraft, kind: str, payload: str, **fields: object) -> TimelineItem:
    return TimelineItem.model_validate(
        {
            **fields,
            "item_id": "pending",
            "kind": kind,
            "payload": payload,
            "created_at": draft.at,
        }
    )


def _first_str(mapping: Mapping[str, object], *keys: str) -> str | None:
    return next((val for k in keys if isinstance(val := mapping.get(k), str) and val), None)


def _format_write_tool_use(tool_input: Mapping[str, object]) -> str:
    path = _first_str(tool_input, "file_path", "Path", "TargetFile")
    content = _first_str(tool_input, "content", "Content", "CodeContent")
    return f"# {path}\n{content.rstrip()}" if path and content is not None else ""


def _format_edit_tool_use(tool_input: Mapping[str, object]) -> str:
    path = _first_str(tool_input, "file_path", "Path", "TargetFile")
    old = _first_str(tool_input, "old_string", "Target", "TargetContent")
    new = _first_str(tool_input, "new_string", "Replacement", "ReplacementContent")
    if path and old is not None and isinstance(new, str):
        return f"# {path}\n--- old\n{old.rstrip()}\n+++ new\n{new.rstrip()}"
    return ""


def format_tool_use(name: str, tool_input: Mapping[str, object]) -> str:
    if (
        name in {"Bash", "command", "run_command"}
        and (cmd := _first_str(tool_input, "command", "CommandLine"))
        and cmd.strip()
    ):
        return cmd
    if name == "patch" and (rendered := _format_patch_tool_use(tool_input)):
        return rendered
    if name in {"Write", "write_file", "write_to_file"} and (
        r := _format_write_tool_use(tool_input)
    ):
        return r
    if name in {"Edit", "edit_file", "replace_file_content"} and (
        r := _format_edit_tool_use(tool_input)
    ):
        return r
    return json.dumps(dict(tool_input), ensure_ascii=False, indent=2) if tool_input else name


def _is_object_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def is_object_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _format_patch_change(raw_entry: object) -> str | None:
    if not _is_object_mapping(raw_entry):
        return None
    entry: dict[str, object] = {str(k): v for k, v in raw_entry.items()}
    if not (path := _first_str(entry, "path")):
        return None
    header = f"# {kind}: {path}" if (kind := _first_str(entry, "kind")) else f"# {path}"
    diff = entry.get("diff")
    return f"{header}\n{diff.rstrip()}" if isinstance(diff, str) and diff.strip() else header


def _format_patch_tool_use(tool_input: Mapping[str, object]) -> str:
    changes = tool_input.get("changes")
    if is_object_sequence(changes) and changes:
        blocks = [r for raw in changes if (r := _format_patch_change(raw)) is not None]
        if blocks:
            return "\n\n".join(blocks)
    raw_paths = tool_input.get("paths")
    if is_object_sequence(raw_paths) and raw_paths and all(isinstance(x, str) for x in raw_paths):
        return "\n".join(x for x in raw_paths if isinstance(x, str))
    return ""
