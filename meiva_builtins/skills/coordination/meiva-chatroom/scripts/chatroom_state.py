from __future__ import annotations

from dataclasses import dataclass, replace

import chatroom_fence as fence

USER = "user"


@dataclass(frozen=True)
class Member:
    agent_id: int
    label: str
    joined_seq: int


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: str
    sender: str
    label: str
    mentions: tuple[int, ...]
    mention_all: bool
    body: str


@dataclass(frozen=True)
class Wake:
    target: int
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class RoomState:
    title: str
    host_agent: int
    host_machine: str
    seq: int
    members: dict[int, Member]
    cursors: dict[int, int]
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class PostRequest:
    sender: str
    label: str | None
    mentions: tuple[int, ...]
    mention_all: bool
    body: str
    ts: str
    caller: int | None


def new_state(
    *, title: str, host_agent: int, host_machine: str, host_label: str, members: dict[int, str]
) -> RoomState:
    roster = {host_agent: host_label, **members}
    return RoomState(
        title=title,
        host_agent=host_agent,
        host_machine=host_machine,
        seq=0,
        members={i: Member(i, label, 0) for i, label in roster.items()},
        cursors=dict.fromkeys(roster, 0),
        entries=(),
    )


def with_member(state: RoomState, agent_id: int, label: str, *, from_start: bool) -> RoomState:
    if agent_id in state.members:
        raise ValueError(f"agent {agent_id} is already a member of this room")
    cursor = 0 if from_start else state.seq
    return replace(
        state,
        members={**state.members, agent_id: Member(agent_id, label, cursor)},
        cursors={**state.cursors, agent_id: cursor},
    )


def plan_post(state: RoomState, request: PostRequest) -> tuple[RoomState, Entry, tuple[Wake, ...]]:
    sender_id = _check_request(state, request)
    seq = state.seq + 1
    entry = Entry(
        seq=seq,
        ts=request.ts,
        sender=request.sender,
        label=_sender_label(state, sender_id, request.label),
        mentions=tuple(dict.fromkeys(request.mentions)),
        mention_all=request.mention_all,
        body=fence.close_open_fence(request.body.rstrip("\n")),
    )
    pool = tuple(state.members) if request.mention_all else entry.mentions
    targets = tuple(t for t in pool if t not in (sender_id, request.caller))
    entries = (*state.entries, entry)
    wakes = tuple(
        Wake(t, tuple(e for e in entries if state.cursors[t] < e.seq <= seq)) for t in targets
    )
    cursors = dict(state.cursors)
    if sender_id is not None:
        cursors[sender_id] = seq
    return replace(state, seq=seq, cursors=cursors, entries=entries), entry, wakes


def with_acks(state: RoomState, acks: dict[int, int]) -> RoomState:
    unknown = [a for a in acks if a not in state.members]
    if unknown:
        raise ValueError(f"acknowledged agents are not members of this room: {unknown}")
    beyond = [a for a, seq in acks.items() if seq > state.seq]
    if beyond:
        raise ValueError(f"cannot acknowledge beyond seq {state.seq}: {beyond}")
    cursors = {**state.cursors, **{a: max(state.cursors[a], seq) for a, seq in acks.items()}}
    return replace(state, cursors=cursors)


def _check_request(state: RoomState, request: PostRequest) -> int | None:
    if not request.body.strip():
        raise ValueError("a message needs a non-empty body")
    sender_id = None if request.sender == USER else _agent_id(request.sender)
    if sender_id is not None and sender_id not in state.members:
        raise ValueError(f"sender {sender_id} is not a member of this room; add-member first")
    unknown = [m for m in request.mentions if m not in state.members]
    if unknown:
        raise ValueError(f"mentioned agents are not members of this room: {unknown}")
    return sender_id


def select_entries(
    entries: tuple[Entry, ...], *, since: int, tail: int | None
) -> tuple[Entry, ...]:
    chosen = tuple(e for e in entries if e.seq > since)
    return chosen if tail is None else chosen[max(len(chosen) - tail, 0) :]


def _agent_id(sender: str) -> int:
    if not sender.isdecimal():
        raise ValueError(f"--from must be an agent id or {USER!r}, got {sender!r}")
    return int(sender)


def _sender_label(state: RoomState, sender_id: int | None, label: str | None) -> str:
    if label:
        return label
    return "User" if sender_id is None else state.members[sender_id].label
