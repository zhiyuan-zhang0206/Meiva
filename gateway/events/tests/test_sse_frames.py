# meiva:note
# The frame decisions of a quiet stream, over plain values.

from __future__ import annotations

from gateway.events.sse_frames import HEARTBEAT_FRAME, SseCadence, warm_frame

CADENCE = SseCadence(heartbeat_s=15.0, keepalive_s=1.0)


def test_a_stream_that_just_spoke_stays_silent() -> None:
    assert warm_frame(100.0, 100.0, 100.0, CADENCE) == (None, 100.0, 100.0)
    assert warm_frame(100.9, 100.0, 100.0, CADENCE) == (None, 100.0, 100.0)


def test_a_keepalive_comment_is_due_once_per_interval() -> None:
    assert warm_frame(101.0, 100.0, 100.0, CADENCE) == (b": hb\n\n", 101.0, 100.0)


def test_polls_shorter_than_the_interval_do_not_postpone_the_comment() -> None:
    last_comment, last_data = 100.0, 100.0
    sent: list[float] = []
    # meiva:note
    # a poll every 250 ms for three seconds
    for tick in range(1, 13):
        now = 100.0 + tick * 0.25
        frame, last_comment, last_data = warm_frame(now, last_comment, last_data, CADENCE)
        if frame is not None:
            sent.append(now)
    assert sent == [101.0, 102.0, 103.0]


def test_silence_past_the_heartbeat_interval_sends_the_visible_heartbeat() -> None:
    frame, last_comment, last_data = warm_frame(115.0, 114.5, 100.0, CADENCE)
    assert (frame, last_comment, last_data) == (HEARTBEAT_FRAME, 115.0, 115.0)


def test_the_cadence_is_a_value() -> None:
    quick = SseCadence(heartbeat_s=0.3, keepalive_s=0.1)
    assert warm_frame(0.3, 0.25, 0.0, quick)[0] == HEARTBEAT_FRAME
    assert warm_frame(0.2, 0.0, 0.0, quick)[0] == b": hb\n\n"
