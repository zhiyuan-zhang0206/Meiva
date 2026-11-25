from __future__ import annotations

import chatroom_state as cs


def render_entry(entry: cs.Entry) -> str:
    sender = entry.label if entry.sender == cs.USER else f"{entry.label} (#{entry.sender})"
    if entry.mention_all:
        target = " -> @all"
    elif entry.mentions:
        target = " -> " + ", ".join(f"@{m}" for m in entry.mentions)
    else:
        target = ""
    return f"\n---\n### #{entry.seq:03d} · {entry.ts} · {sender}{target}\n\n{entry.body}\n"


def render_room_header(state: cs.RoomState, *, created: str) -> str:
    rows = "\n".join(
        f"| #{m.agent_id} | {m.label} | #{m.joined_seq:03d} |" for m in state.members.values()
    )
    return (
        f"# {state.title}\n\n"
        f"Host: #{state.host_agent} on `{state.host_machine}` · created {created}\n\n"
        "Append-only roundtable: entries are added with `room.py post`, never edited.\n\n"
        f"| Agent | Label | Joined at |\n|---|---|---|\n{rows}\n\n"
    )


def render_join_note(member: cs.Member, *, ts: str) -> str:
    return (
        f"\n> {ts} · {member.label} (#{member.agent_id}) joined "
        f"(reads from #{member.joined_seq + 1:03d})\n\n"
    )


def render_status(state: cs.RoomState) -> str:
    lines = [
        f"title: {state.title}",
        f"host: #{state.host_agent} on {state.host_machine}",
        f"seq: {state.seq}",
        "members:",
        *(
            f"  #{m.agent_id} {m.label} joined_seq={m.joined_seq} cursor={state.cursors[m.agent_id]}"
            for m in state.members.values()
        ),
    ]
    return "\n".join(lines) + "\n"
