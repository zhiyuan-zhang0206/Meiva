from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from base.lm.harness.codex.frames import History
from base.lm.harness.codex.requests import open_thread_request
from base.lm.harness.codex.tests.fake_app_server import FakeAppServer
from base.lm.harness.codex.tests.support import start

# meiva:note
# What the Codex session does for a reader of its own record, and that it does nothing when there is none. With
# `CodexConfig.transcript` unset the requests are exactly the ones the adapter always sent (a resume asks for the
# thread with its history, as before); with it set the resume asks for the thread without its turns, the session
# hands its connection and the thread's rollout path and CLI version to the tap, gives it every notification and
# takes the connection back when it closes or the server is gone.


class RecordingTap:
    def __init__(self) -> None:
        self.attached: list[tuple[History, str | None, str | None]] = []
        self.detached = 0
        self.notifications: list[tuple[str, Mapping[str, object]]] = []

    def attach(
        self, history: History, *, rollout_path: str | None, cli_version: str | None
    ) -> None:
        self.attached.append((history, rollout_path, cli_version))

    def detach(self) -> None:
        self.detached += 1

    def on_notification(self, method: str, params: Mapping[str, object]) -> None:
        self.notifications.append((method, params))


def test_the_open_requests_are_what_they_always_were_unless_the_timeline_reads_the_transcript() -> (
    None
):
    base = {
        "cwd": "/w",
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "config": {"shell_environment_policy": {"inherit": "all"}},
    }

    def request(resume: str | None, **more: bool) -> tuple[str, dict[str, object]]:
        return open_thread_request(
            resume,
            cwd="/w",
            model=None,
            approval_policy="never",
            sandbox="danger-full-access",
            **more,
        )

    assert request("thr") == ("thread/resume", {"threadId": "thr", **base})
    assert request("thr", exclude_turns=False) == ("thread/resume", {"threadId": "thr", **base})
    assert request("thr", exclude_turns=True) == (
        "thread/resume",
        {"threadId": "thr", **base, "excludeTurns": True},
    )
    assert request(None, exclude_turns=True) == ("thread/start", base)


async def test_without_a_tap_the_session_resumes_with_the_history_as_before(tmp_path: Path) -> None:
    server = FakeAppServer()
    harness = await start(tmp_path, server=server, resume="thread-old")
    try:
        assert "excludeTurns" not in server.params_for("thread/resume")[0]
    finally:
        await harness.stop()


async def test_with_a_tap_the_resume_leaves_out_the_history_and_the_tap_gets_the_connection(
    tmp_path: Path,
) -> None:
    server = FakeAppServer()
    tap = RecordingTap()
    harness = await start(tmp_path, server=server, resume="thread-old", transcript=tap)
    try:
        assert server.params_for("thread/resume")[0]["excludeTurns"] is True
        assert tap.attached == [(server, "/codex/sessions/rollout-fake.jsonl", "0.0.0-fake")]
        server.emit("item/completed", {"threadId": "thread-old", "turnId": "t", "item": {}})
        server.emit("thread/tokenUsage/updated", {"threadId": "thread-old"})
        assert [method for method, _ in tap.notifications] == [
            "item/completed",
            "thread/tokenUsage/updated",
        ]
        assert tap.detached == 0
    finally:
        await harness.stop()
    assert tap.detached == 1
    assert set(server.methods()) <= {"initialize", "thread/resume"}


async def test_a_new_thread_is_started_as_before_and_the_tap_is_attached(tmp_path: Path) -> None:
    server = FakeAppServer()
    tap = RecordingTap()
    harness = await start(tmp_path, server=server, transcript=tap)
    try:
        assert "excludeTurns" not in server.params_for("thread/start")[0]
        assert len(tap.attached) == 1
    finally:
        await harness.stop()


async def test_the_tap_takes_the_connection_back_when_the_server_goes_away(tmp_path: Path) -> None:
    server = FakeAppServer()
    tap = RecordingTap()
    harness = await start(tmp_path, server=server, transcript=tap)
    try:
        await server.close()
        assert tap.detached >= 1
    finally:
        await harness.stop()
