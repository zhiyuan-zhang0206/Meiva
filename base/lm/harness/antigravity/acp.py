from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.delivery import InputBlock, TextBlock
from base.lm.harness.events import HarnessCause, ToolResult, ToolUse


def prompt_blocks(blocks: Sequence[InputBlock]) -> list[dict[str, str]]:
    return [
        {"type": "text", "text": block.text}
        if isinstance(block, TextBlock)
        else {"type": "image", "mimeType": block.mime_type, "data": block.data_base64}
        for block in blocks
    ]


_LEGACY_MARKER = re.compile(r"<!-- meiva-input:([a-f0-9]{64}) -->")


def split_legacy_marker(text: str) -> tuple[str | None, str]:
    first, _, rest = text.partition("\n")
    found = _LEGACY_MARKER.fullmatch(first)
    return (found.group(1), rest.strip()) if found else (None, text)


def content_text(content: object) -> str:
    if is_object_dict(content) and content.get("type") == "text":
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


@dataclass(frozen=True, slots=True)
class ToolCall:
    use: ToolUse

    @classmethod
    def from_update(cls, update: dict[str, object], *, turn_id: str, tool_id: str) -> ToolCall:
        name = update.get("name") or update.get("title")
        raw = update.get("rawInput", update.get("args"))
        return cls(
            ToolUse(
                turn_id,
                tool_id,
                name if isinstance(name, str) else "unknown",
                raw if is_object_dict(raw) else {},
            )
        )

    def result(self, update: dict[str, object]) -> ToolResult | None:
        status = update.get("status")
        if status not in ("completed", "failed"):
            return None
        output = update.get("rawOutput")
        if is_object_dict(output):
            exit_code = output.get("exit_code")
            is_error = bool(output.get("error")) or (type(exit_code) is int and exit_code != 0)
        else:
            is_error = False
        chunks = update.get("content")
        text = ""
        if is_object_list(chunks):
            text = "\n".join(
                content_text(chunk.get("content")) for chunk in chunks if is_object_dict(chunk)
            )
        if not text and output is not None:
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return ToolResult(
            self.use.turn_id, self.use.tool_use_id, status == "failed" or is_error, text
        )


class UnrecognisedError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


_RESTART_MARKER = "have been stopped due to server restart"
_REMINDER_BLOCK = re.compile(
    r"\A(?:The following is a <SYSTEM_MESSAGE>[^\n]*\n)?"
    r"\s*<SYSTEM_MESSAGE>\s*Reminder:[ \t]*(?!\s*</?SYSTEM_MESSAGE>)\S"
    r"(?:(?!</?SYSTEM_MESSAGE>).)*</SYSTEM_MESSAGE>\s*\Z",
    re.DOTALL,
)
_ENVELOPE = re.compile(
    r"\A\[Message\] timestamp=(\S+) sender=(\S+) priority=(\S+) content=(.*)\Z", re.DOTALL
)
_TASK_SENDER = re.compile(r"/task-\d+\Z")
_TASK_FINISHED = 'Task id "'


@dataclass(frozen=True, slots=True)
class SystemNotice:
    text: str
    cause: HarnessCause
    sender: str


def classify_system_message(content: str) -> SystemNotice | None:
    if "[Notice]" in content and _RESTART_MARKER in content:
        return None
    wrapped = re.search(
        r"<SYSTEM_MESSAGE>\s*(\[Message\].*?)\s*</SYSTEM_MESSAGE>", content, re.DOTALL
    )
    envelope = wrapped.group(1) if wrapped is not None else content
    found = _ENVELOPE.match(envelope)
    if found is None:
        if wrapped is None and _REMINDER_BLOCK.match(content) is not None:
            return None
        raise UnrecognisedError(f"system_message:{envelope.split(maxsplit=1)[0]}")
    sender, text = found.group(2), found.group(4).strip()
    if sender == "system":
        return None
    if not text:
        raise UnrecognisedError("system_message:empty")
    return SystemNotice(text=text, cause=_cause(sender, text), sender=sender)


def _cause(sender: str, text: str) -> HarnessCause:
    if _TASK_SENDER.search(sender):
        return (
            HarnessCause.BACKGROUND_TASK
            if text.startswith(_TASK_FINISHED)
            else HarnessCause.SCHEDULED_WAKE
        )
    if sender == "system":
        return HarnessCause.UNKNOWN
    return HarnessCause.BACKGROUND_TASK
