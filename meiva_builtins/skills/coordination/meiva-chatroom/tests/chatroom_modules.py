from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from meiva_builtins.skills.tests.skill_script import load_skill_script


@dataclass(frozen=True)
class ChatroomModules:
    fence: ModuleType
    state: ModuleType
    render: ModuleType
    serial: ModuleType
    files: ModuleType
    wake: ModuleType
    room: ModuleType


def _load(stem: str, name: str) -> ModuleType:
    return load_skill_script("coordination", "meiva-chatroom", "scripts", f"{stem}.py", name=name)


def load_chatroom_modules() -> ChatroomModules:
    fence = _load("chatroom_fence", "chatroom_fence")
    state = _load("chatroom_state", "chatroom_state")
    render = _load("chatroom_render", "chatroom_render")
    serial = _load("chatroom_serial", "chatroom_serial")
    files = _load("chatroom_files", "chatroom_files")
    wake = _load("chatroom_wake", "chatroom_wake")
    room = _load("room", "room")
    return ChatroomModules(fence, state, render, serial, files, wake, room)
