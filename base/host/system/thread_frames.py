# meiva:note
# Reading what a thread of this process is executing, as a value.
#
# A watchdog that judges a stall samples a thread's current frame on another thread. The frame
# is a live interpreter object that no test can build on demand, and "the read failed" has no
# real producer; `FrameReader` is the narrow seam. `SystemFrameReader` reads the interpreter's
# own frames, and `base.host.system.tests.scripted_frames.ScriptedFrameReader` answers from a script.

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol

__all__ = ["FrameFacts", "FrameReader", "SystemFrameReader"]


# meiva:note
# The facts of a thread's innermost frame and of its caller that a park/stall judgment reads.
@dataclass(frozen=True)
class FrameFacts:
    filename: str
    lineno: int
    function: str
    parent_filename: str | None
    parent_function: str | None


# meiva:note
# The innermost frame of `thread_id` now, or None when that thread has no frame (it ended).
class FrameReader(Protocol):
    def read(self, thread_id: int) -> FrameFacts | None: ...


# meiva:note
# The interpreter's own frames (`sys._current_frames`).
class SystemFrameReader:
    def read(self, thread_id: int) -> FrameFacts | None:
        frame = sys._current_frames().get(thread_id)
        if frame is None:
            return None
        parent = frame.f_back
        return FrameFacts(
            filename=frame.f_code.co_filename,
            lineno=frame.f_lineno,
            function=frame.f_code.co_name,
            parent_filename=None if parent is None else parent.f_code.co_filename,
            parent_function=None if parent is None else parent.f_code.co_name,
        )
