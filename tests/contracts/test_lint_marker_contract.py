from __future__ import annotations

import ast
import inspect
from pathlib import Path
from uuid import uuid4

import pytest

from base.agents.messages.kwargs import NoteTag

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYSTEM_NOTE_WRITER_DIRS = ("agent", "meiva_builtins", "demos")


def _note_writer_sources() -> list[Path]:
    return [
        path
        for directory in _SYSTEM_NOTE_WRITER_DIRS
        for path in (_REPO_ROOT / directory).rglob("*.py")
        if "tests" not in path.relative_to(_REPO_ROOT).parts
    ]


def _system_note_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(), filename=path)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "system_note_message"
    ]


def _is_notetag_member(tag: ast.expr) -> bool:
    return (
        isinstance(tag, ast.Attribute)
        and isinstance(tag.value, ast.Name)
        and tag.value.id == "NoteTag"
    )


def _is_validated_inbound_tag(tag: ast.expr) -> bool:
    return (
        isinstance(tag, ast.Call)
        and isinstance(tag.func, ast.Name)
        and tag.func.id == "_system_note_tag"
    )


def test_system_note_writers_use_notetag_values() -> None:
    for path in _note_writer_sources():
        for node in _system_note_calls(path):
            tag = next((kw.value for kw in node.keywords if kw.arg == "tag"), None)
            assert tag is not None, f"{path.relative_to(_REPO_ROOT)}:{node.lineno} lacks tag="
            assert _is_notetag_member(tag) or _is_validated_inbound_tag(tag), (
                f"{path.relative_to(_REPO_ROOT)}:{node.lineno} must pass tag=NoteTag.<member> "
                "or the validated inbound NoteTag helper"
            )


def test_send_system_note_default_tag_is_live_notetag_value() -> None:
    from meiva.agents import send_system_note

    default = inspect.signature(send_system_note).parameters["tag"].default
    assert isinstance(default, str)
    assert default in {tag.value for tag in NoteTag}


def test_send_system_note_rejects_unknown_notetag_before_gateway() -> None:
    from meiva.agents import peers

    with pytest.raises(ValueError) as exc_info:
        peers.validate_system_note(
            7,
            "note",
            tag="unrecognized",
            task_id=None,
            resurrect=True,
            idempotency_key=str(uuid4()),
        )
    assert "task" in str(exc_info.value)
    assert "lifecycle_fork" in str(exc_info.value)
