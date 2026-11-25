from __future__ import annotations

import json

import chatroom_json as cj
import chatroom_state as cs

SCHEMA_VERSION = 1


def dump_state(state: cs.RoomState) -> str:
    return json.dumps(
        {
            "version": SCHEMA_VERSION,
            "title": state.title,
            "host_agent": state.host_agent,
            "host_machine": state.host_machine,
            "seq": state.seq,
            "members": {
                str(m.agent_id): {"label": m.label, "joined_seq": m.joined_seq}
                for m in state.members.values()
            },
            "cursors": {str(i): c for i, c in state.cursors.items()},
            "messages": [entry_to_json(e) for e in state.entries],
        },
        ensure_ascii=False,
        indent=1,
    )


def load_state(text: str) -> cs.RoomState:
    raw = cj.as_object(cj.parse_json(text))
    if raw.get("version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported room state version: {raw.get('version')!r}")
    members = cj.as_object(raw["members"])
    return cs.RoomState(
        title=cj.as_str(raw["title"]),
        host_agent=cj.as_int(raw["host_agent"]),
        host_machine=cj.as_str(raw["host_machine"]),
        seq=cj.as_int(raw["seq"]),
        members={
            int(i): cs.Member(int(i), cj.as_str(m["label"]), cj.as_int(m["joined_seq"]))
            for i, m in ((i, cj.as_object(v)) for i, v in members.items())
        },
        cursors={int(i): cj.as_int(c) for i, c in cj.as_object(raw["cursors"]).items()},
        entries=tuple(entry_from_json(cj.as_object(m)) for m in cj.as_list(raw["messages"])),
    )


def entry_to_json(entry: cs.Entry) -> dict[str, object]:
    return {
        "seq": entry.seq,
        "ts": entry.ts,
        "sender": entry.sender,
        "label": entry.label,
        "mentions": list(entry.mentions),
        "mention_all": entry.mention_all,
        "body": entry.body,
    }


def entry_from_json(raw: dict[str, object]) -> cs.Entry:
    return cs.Entry(
        seq=cj.as_int(raw["seq"]),
        ts=cj.as_str(raw["ts"]),
        sender=cj.as_str(raw["sender"]),
        label=cj.as_str(raw["label"]),
        mentions=tuple(cj.as_int(m) for m in cj.as_list(raw["mentions"])),
        mention_all=bool(raw["mention_all"]),
        body=cj.as_str(raw["body"]),
    )
