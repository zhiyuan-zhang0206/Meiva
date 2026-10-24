# meiva:note
# Finite words emitted by the host probe and helper-signing facts protocol. Unknown words are
# malformed evidence, never a negative answer that can let a window continue.

from enum import StrEnum


class HelperPresence(StrEnum):
    ABSENT = "absent"
    PRESENT = "present"


class ProbeFlag(StrEnum):
    NO = "no"
    YES = "yes"


def helper_presence(value: str) -> HelperPresence:
    try:
        return HelperPresence(value)
    except ValueError as exc:
        raise ValueError(f"invalid helper probe value: {value!r}") from exc


def probe_flag(field: str, value: str) -> ProbeFlag:
    try:
        return ProbeFlag(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field} probe value: {value!r}") from exc
