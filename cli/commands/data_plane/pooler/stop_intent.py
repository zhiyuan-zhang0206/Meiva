# meiva:note
# The durable record that a PgBouncer birth was asked to stop, as values.
#
# PgBouncer escalates a second SIGINT or SIGTERM to immediate shutdown, so the exact native
# birth is persisted before the first signal and a retry only waits for it to exit. A record
# that cannot be read as exactly that shape never authorizes another signal.

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeGuard

__all__ = ["NativeBirth", "intent_names", "native_birth", "render_intent"]

_REFUSAL = "invalid PgBouncer stop intent; custody retained"
_KEYS = ("pid", "birth", "starttime")


# meiva:note
# One native process birth as the intent file records it. On Linux the kernel start tick is
# the identity and the wall-clock `birth` is not recorded, so a correction of the wall clock
# between two readers cannot turn one birth into two.
@dataclass(frozen=True)
class NativeBirth:
    pid: int
    birth: float | None
    starttime: int | None


# meiva:note
# The birth a process identity records: `birth` only where there is no start tick.
def native_birth(pid: int, birth: float, starttime: int | None) -> NativeBirth:
    return NativeBirth(pid, birth if starttime is None else None, starttime)


# meiva:note
# The intent file's text for `birth`.
def render_intent(birth: NativeBirth) -> str:
    return json.dumps({"pid": birth.pid, "birth": birth.birth, "starttime": birth.starttime})


def _number(value: object) -> bool:
    return type(value) in (int, float)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# Whether the intent file's text names exactly `birth`.
#
# Any other readable shape (a different birth) is False. Text that is not the recorded shape
# (wrong keys, wrong types, a birth/starttime pair that is neither of the two valid forms)
# raises, because custody is retained instead of guessed.
def intent_names(text: str, birth: NativeBirth) -> bool:
    try:
        raw: object = json.loads(text)
    except ValueError:
        raise RuntimeError(_REFUSAL) from None
    value = raw if _is_object_dict(raw) else None
    if value is None or sorted(value) != sorted(_KEYS) or type(value["pid"]) is not int:
        raise RuntimeError(_REFUSAL)
    recorded_birth, recorded_ticks = value["birth"], value["starttime"]
    on_ticks = type(recorded_ticks) is int and recorded_birth is None
    on_birth = recorded_ticks is None and _number(recorded_birth)
    if not (on_ticks or on_birth):
        raise RuntimeError(_REFUSAL)
    return (value["pid"], recorded_birth, recorded_ticks) == (
        birth.pid,
        birth.birth,
        birth.starttime,
    )
