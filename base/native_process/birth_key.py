from __future__ import annotations


def process_birth_key(
    pid: int, birth: float, starttime: int | None, *, platform: str
) -> tuple[int, str, int | float]:
    # meiva:note
    # The exact native key; Linux never substitutes a wall-clock timestamp.
    if starttime is not None:
        if type(starttime) is not int or starttime <= 0:
            raise ValueError("native start ticks must be a positive integer")
        return pid, "ticks", starttime
    if platform == "linux":
        raise RuntimeError(f"missing Linux start ticks for PID {pid}")
    return pid, "timestamp", birth
