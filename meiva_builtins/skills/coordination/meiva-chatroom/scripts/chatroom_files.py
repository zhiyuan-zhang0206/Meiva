from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import chatroom_render as cr
import chatroom_serial as ss
import chatroom_state as cs


@dataclass(frozen=True)
class RoomFiles:
    md: Path
    state: Path
    lock: Path

    @classmethod
    def at(cls, md_path: str) -> RoomFiles:
        md = Path(md_path).expanduser().absolute()
        if md.suffix != ".md":
            raise ValueError(f"a room file ends in .md, got {md}")
        state_dir = md.parent / ".state"
        return cls(md, state_dir / f"{md.stem}.json", state_dir / f"{md.stem}.lock")


@dataclass(frozen=True)
class PostOutcome:
    state: cs.RoomState
    entry: cs.Entry
    wakes: tuple[cs.Wake, ...]


class _Locked:
    def __init__(self, files: RoomFiles) -> None:
        self._path = files.lock
        self._handle: TextIO | None = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a")
        fcntl.flock(self._handle, fcntl.LOCK_EX)

    def __exit__(self, *_exc: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()


def _locked(files: RoomFiles) -> _Locked:
    return _Locked(files)


def load(files: RoomFiles) -> cs.RoomState:
    if not files.state.exists():
        raise FileNotFoundError(f"no room at {files.md} (run `room.py init` first)")
    return ss.load_state(files.state.read_text(encoding="utf-8"))


def _save(files: RoomFiles, state: cs.RoomState) -> None:
    temp = files.state.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        _ = handle.write(ss.dump_state(state))
        handle.flush()
        os.fsync(handle.fileno())
    _ = temp.replace(files.state)


def _append(files: RoomFiles, text: str) -> None:
    with files.md.open("a", encoding="utf-8") as handle:
        _ = handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def init_room(files: RoomFiles, state: cs.RoomState, *, created: str, force: bool) -> None:
    with _locked(files):
        if (files.md.exists() or files.state.exists()) and not force:
            raise FileExistsError(f"room already exists at {files.md}; pass --force to replace it")
        files.md.parent.mkdir(parents=True, exist_ok=True)
        _save(files, state)
        _ = files.md.write_text(cr.render_room_header(state, created=created), encoding="utf-8")


def add_member(
    files: RoomFiles, agent_id: int, label: str, *, from_start: bool, ts: str
) -> cs.RoomState:
    with _locked(files):
        state = cs.with_member(load(files), agent_id, label, from_start=from_start)
        _append(files, cr.render_join_note(state.members[agent_id], ts=ts))
        _save(files, state)
    return state


def post(files: RoomFiles, request: cs.PostRequest) -> PostOutcome:
    with _locked(files):
        state, entry, wakes = cs.plan_post(load(files), request)
        _append(files, cr.render_entry(entry))
        _save(files, state)
    return PostOutcome(state, entry, wakes)


def ack_cursors(files: RoomFiles, acks: dict[int, int]) -> cs.RoomState:
    with _locked(files):
        state = cs.with_acks(load(files), acks)
        _save(files, state)
    return state
