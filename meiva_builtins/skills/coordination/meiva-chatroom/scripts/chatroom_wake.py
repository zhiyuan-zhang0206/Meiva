from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import chatroom_json as cj
import chatroom_render as cr
import chatroom_serial as ss
import chatroom_state as cs

REPLY_COMMAND = '"$MEIVA_PYTHON" ~/.meiva/skills/meiva-chatroom/scripts/room.py'

Sender = Callable[[int, str], None]


@dataclass(frozen=True)
class PostPlan:
    seq: int
    title: str
    host_agent: int
    host_machine: str
    room_path: str
    wakes: tuple[cs.Wake, ...]

    @property
    def room_spec(self) -> str:
        return f"{self.host_machine}:{self.room_path}"

    def to_json(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "title": self.title,
            "host_agent": self.host_agent,
            "host_machine": self.host_machine,
            "room_path": self.room_path,
            "wakes": [
                {"target": w.target, "entries": [ss.entry_to_json(e) for e in w.entries]}
                for w in self.wakes
            ],
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> PostPlan:
        return cls(
            seq=cj.as_int(raw["seq"]),
            title=cj.as_str(raw["title"]),
            host_agent=cj.as_int(raw["host_agent"]),
            host_machine=cj.as_str(raw["host_machine"]),
            room_path=cj.as_str(raw["room_path"]),
            wakes=tuple(_wake_from_json(cj.as_object(w)) for w in cj.as_list(raw["wakes"])),
        )


def _wake_from_json(raw: dict[str, object]) -> cs.Wake:
    entries = tuple(ss.entry_from_json(cj.as_object(e)) for e in cj.as_list(raw["entries"]))
    return cs.Wake(cj.as_int(raw["target"]), entries)


def render_wake(plan: PostPlan, wake: cs.Wake) -> str:
    entries = wake.entries
    newest = entries[-1]
    spec = plan.room_spec
    reply_to = "" if newest.sender == cs.USER else f" --mention {newest.sender}"
    return (
        f'[meiva-chatroom] New messages in "{plan.title}" '
        f"(host #{plan.host_agent} on {plan.host_machine})\n"
        f"Room: {spec}\n"
        f"Unread for you: #{entries[0].seq:03d}..#{newest.seq:03d}\n\n"
        + "\n".join(cr.render_entry(e) for e in entries)
        + "\nReply by appending (never edit the .md directly); the heredoc keeps quotes, `$` and "
        "multi-line text intact:\n"
        f"{REPLY_COMMAND} post --room {spec} --from {wake.target}{reply_to} --stdin <<'CHATROOM_EOF'\n"
        "<your reply>\n"
        "CHATROOM_EOF\n"
        "Add --mention <id> to pass the mic; omit it to hand the floor back. "
        "If you have nothing new to add, do not post.\n"
    )


def missed_range(wake: cs.Wake) -> list[int]:
    return [wake.entries[0].seq, wake.entries[-1].seq]


def deliver(plan: PostPlan, send: Sender) -> dict[int, str]:
    failed: dict[int, str] = {}
    for wake in plan.wakes:
        try:
            send(wake.target, render_wake(plan, wake))
        except Exception as error:
            failed[wake.target] = f"{type(error).__name__}: {error}"
    return failed
