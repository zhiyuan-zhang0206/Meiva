# meiva:note
# A `FrameReader` that answers from a script, for the tests of a stall watchdog.
#
# Each `read` returns the next scripted value: a `FrameFacts` or None, or raises a scripted
# exception (the failed read no real interpreter can produce). The last entry repeats.

from __future__ import annotations

from collections.abc import Sequence

from base.host.system.thread_frames import FrameFacts

__all__ = ["ScriptedFrameReader"]


class ScriptedFrameReader:
    def __init__(self, script: Sequence[FrameFacts | BaseException | None]) -> None:
        if not script:
            raise ValueError("a scripted frame reader needs at least one entry")
        self._script = list(script)
        self.reads: list[int] = []

    def read(self, thread_id: int) -> FrameFacts | None:
        self.reads.append(thread_id)
        entry = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(entry, BaseException):
            raise entry
        return entry
