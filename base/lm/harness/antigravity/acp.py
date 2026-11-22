from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.delivery import InputBlock, TextBlock
from base.lm.harness.events import ToolResult, ToolUse


def input_marker(key: str) -> str:
    return f"<!-- meiva-input:{hashlib.sha256(key.encode()).hexdigest()} -->"


def prompt_blocks(key: str, blocks: Sequence[InputBlock]) -> list[dict[str, str]]:
    marker = input_marker(key)
    content: list[dict[str, str]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            text = block.text if content else f"{marker}\n{block.text}"
            content.append({"type": "text", "text": text})
        else:
            if not content:
                content.append({"type": "text", "text": marker})
            content.append(
                {"type": "image", "mimeType": block.mime_type, "data": block.data_base64}
            )
    return content


_INPUT_MARKER = re.compile(r"<!-- meiva-input:[a-f0-9]{64} -->")


def without_input_marker(text: str) -> str:
    first, _, rest = text.partition("\n")
    return rest.strip() if _INPUT_MARKER.fullmatch(first) else text


def received_markers(rows: Sequence[dict[str, object]]) -> set[str]:
    markers: set[str] = set()
    for row in rows:
        content = row.get("content")
        if row.get("type") != "USER_INPUT" or not isinstance(content, str):
            continue
        first_line = content.removeprefix("<USER_REQUEST>\n").split("\n", 1)[0]
        if _INPUT_MARKER.fullmatch(first_line):
            markers.add(first_line)
    return markers


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
