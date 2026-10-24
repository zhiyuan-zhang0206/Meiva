from __future__ import annotations

import threading

import pytest

from base.host.system.tests.scripted_frames import ScriptedFrameReader
from base.host.system.thread_frames import FrameFacts, SystemFrameReader


def test_the_system_reader_sees_the_innermost_frame_of_a_parked_thread() -> None:
    parked = threading.Event()
    release = threading.Event()

    def wait_here() -> None:
        parked.set()
        release.wait(5)

    thread = threading.Thread(target=wait_here)
    thread.start()
    try:
        assert parked.wait(5)
        assert thread.ident is not None
        facts = SystemFrameReader().read(thread.ident)
        assert facts is not None
        assert facts.function == "wait"
        assert facts.parent_function == "wait"
    finally:
        release.set()
        thread.join()


def test_the_system_reader_has_no_frame_for_an_ended_thread() -> None:
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    assert thread.ident is not None
    assert SystemFrameReader().read(thread.ident) is None


def test_a_scripted_reader_replays_its_script_then_repeats_the_last_entry() -> None:
    here = FrameFacts("a.py", 3, "f", None, None)
    reader = ScriptedFrameReader([here, None])
    assert [reader.read(1), reader.read(1), reader.read(2)] == [here, None, None]
    assert reader.reads == [1, 1, 2]


def test_a_scripted_failure_is_raised_to_the_caller() -> None:
    reader = ScriptedFrameReader([RuntimeError("frames unavailable")])
    with pytest.raises(RuntimeError, match="frames unavailable"):
        reader.read(1)
