from __future__ import annotations

import os
from pathlib import Path

from base.lm.harness.antigravity.tests.fake_wire import (
    STAMP,
    after_release,
    background_notice,
    declare_child,
    frame,
    message,
    planner_row,
    record,
    result,
    update,
)


def child_notice(transcript: Path) -> None:
    record(
        transcript,
        {
            "type": "SYSTEM_MESSAGE",
            "content": (
                f"[Message] timestamp={STAMP} sender=child-conv "
                "priority=MESSAGE_PRIORITY_HIGH content=early child callback"
            ),
        },
    )


def barrier() -> None:
    update("usage_update", used=2, size=10)
    frame({"id": "collision-receipt", "method": "test/frame_barrier"})


def early_idle_native(transcript: Path, *, child: bool) -> None:
    if child:
        child_notice(transcript)
    message("NATIVE RESPONSE")
    barrier()

    def finish() -> None:
        if child:
            declare_child(transcript)
        else:
            background_notice(transcript)
        record(transcript, planner_row("NATIVE RESPONSE", tokens=237))
        update("usage_update", used=3, size=10)

    after_release(Path("finish-early-idle"), finish)


def stale_echo(transcript: Path) -> None:
    message("READY")
    barrier()

    def record_native() -> None:
        background_notice(transcript)
        record(transcript, planner_row("READY", tokens=237))
        _ = Path("idle-native-pid").write_text(str(os.getpid()))
        update("usage_update", used=3, size=10)

    after_release(Path("finish-early-idle"), record_native)


def replay_late_idle_echo() -> None:
    if Path("idle-native-pid").read_text() == str(os.getpid()):
        message("READY")
        update("usage_update", used=4, size=10)


def unregistered_child(transcript: Path, *, adjacent: bool, echo_timer: bool) -> None:
    child_notice(transcript)
    record(transcript, planner_row("CHILD RESPONSE", tokens=217))
    message("CHILD RESPONSE")
    if adjacent:
        background_notice(transcript)
        record(transcript, planner_row("TIMER RESPONSE", tokens=237))
        if echo_timer:
            message("TIMER RESPONSE")
    barrier()
    after_release(Path("finish-early-idle"), lambda: declare_child(transcript))


def prepare_idle(transcript: Path, request: int, case: str) -> None:
    reply = "OK" if case in ("child", "drained", "recorded") else "READY"
    record(transcript, planner_row(reply))
    message(reply)
    result(request)

    def send() -> None:
        if case in ("child", "drained"):
            early_idle_native(transcript, child=case == "child")
        elif case == "stale":
            stale_echo(transcript)
        elif case == "recorded":
            background_notice(transcript)
            record(transcript, planner_row("NATIVE RESPONSE", tokens=237))
            message("NATIVE RESPONSE")
            barrier()
        else:
            unregistered_child(
                transcript, adjacent=case != "intervening", echo_timer=case != "fence"
            )

    after_release(Path("send-early-idle"), send)
