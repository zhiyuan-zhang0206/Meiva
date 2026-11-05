from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, TypeGuard


def _first_str(mapping: Mapping[str, Any], *keys: str) -> str | None:
    return next((val for k in keys if isinstance(val := mapping.get(k), str) and val), None)


def _format_write_tool_use(tool_input: Mapping[str, Any]) -> str:
    path = _first_str(tool_input, "file_path", "Path", "TargetFile")
    content = _first_str(tool_input, "content", "Content", "CodeContent")
    return f"# {path}\n{content.rstrip()}" if path and content is not None else ""


def _format_edit_tool_use(tool_input: Mapping[str, Any]) -> str:
    path = _first_str(tool_input, "file_path", "Path", "TargetFile")
    old = _first_str(tool_input, "old_string", "Target", "TargetContent")
    new = _first_str(tool_input, "new_string", "Replacement", "ReplacementContent")
    if path and old is not None and isinstance(new, str):
        return f"# {path}\n--- old\n{old.rstrip()}\n+++ new\n{new.rstrip()}"
    return ""


def format_tool_use(name: str, tool_input: Mapping[str, Any]) -> str:
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
    entry: dict[str, Any] = {str(k): v for k, v in raw_entry.items()}
    if not (path := _first_str(entry, "path")):
        return None
    header = f"# {kind}: {path}" if (kind := _first_str(entry, "kind")) else f"# {path}"
    diff = entry.get("diff")
    return f"{header}\n{diff.rstrip()}" if isinstance(diff, str) and diff.strip() else header


def _format_patch_tool_use(tool_input: Mapping[str, Any]) -> str:
    changes = tool_input.get("changes")
    if is_object_sequence(changes) and changes:
        blocks = [r for raw in changes if (r := _format_patch_change(raw)) is not None]
        if blocks:
            return "\n\n".join(blocks)
    raw_paths = tool_input.get("paths")
    if is_object_sequence(raw_paths) and raw_paths and all(isinstance(x, str) for x in raw_paths):
        return "\n".join(x for x in raw_paths if isinstance(x, str))
    return ""
