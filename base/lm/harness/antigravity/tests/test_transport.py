from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from base.clock.tests.fakes import system_clock
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.tests.support import seed_replay_history, start
from base.lm.harness.antigravity.transport import MAX_FRAME_BYTES, AgyTransport
from base.lm.harness.delivery import HarnessInput
from base.lm.harness.events import (
    AssistantText,
    HarnessCause,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    UnsolicitedOutput,
    Usage,
)
from base.lm.harness.process import PsutilChildProbe

FAKE_CLI = Path(__file__).with_name("fake_agy.py")


def _transport(
    config: AntigravityConfig, *, max_frame_bytes: int = MAX_FRAME_BYTES
) -> AgyTransport:
    return AgyTransport(
        config,
        environ={"HOME": str(config.cwd)},
        owner_uid=os.getuid(),
        generation_id=uuid4().hex,
        clock=system_clock(),
        max_frame_bytes=max_frame_bytes,
    )


def test_argv_selects_acp_and_preserves_permission_policy(tmp_path: Path) -> None:
    assert AntigravityConfig(cwd=tmp_path).command() == ("agy", "acp")
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=("fake",),
        skip_permissions=True,
        extra_args=("--stdio",),
    )
    assert config.command() == ("fake", "acp", "--dangerously-skip-permissions", "--stdio")


async def test_startup_rejects_earlier_cli_that_preempts_steer(tmp_path: Path) -> None:
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=("--test-legacy",),
    )
    transport = _transport(config)
    await transport.start()
    try:
        with pytest.raises(RuntimeError, match=r"lossless ACP steering requires agy 1\.5\.14"):
            _ = await transport.open_session(resume=None)
    finally:
        await transport.close()


async def test_cli_that_cannot_start_acp_reports_version_exit_and_stderr(tmp_path: Path) -> None:
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=("--test-no-acp",),
    )
    transport = _transport(config)
    await transport.start()
    try:
        with pytest.raises(
            RuntimeError, match=r"(?s)requires agy 1\.5\.14.*exit=2.*unknown command acp"
        ):
            _ = await transport.open_session(resume=None)
    finally:
        await transport.close()


@pytest.mark.parametrize("overflow", [False, True])
async def test_stdout_assembles_large_and_unterminated_frames(
    tmp_path: Path, overflow: bool
) -> None:
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=("--test-large-frames",),
    )
    transport = _transport(config, max_frame_bytes=4096 if overflow else 12 * 1024**2)
    await transport.start()
    try:
        assert await transport.open_session(resume=None) == "conv-frames"
        async with asyncio.timeout(5):
            frames = [frame async for frame in transport.events()]
        if overflow:
            assert frames == []
            assert any("stream line exceeded" in line for line in transport.stderr_tail)
        else:
            assert frames == [
                {"jsonrpc": "2.0", "blob": "x" * (10 * 1024**2)},
                {"jsonrpc": "2.0", "tail": "end"},
            ]
    finally:
        await transport.close()


async def test_close_recovers_an_unsampled_child_that_created_its_own_session(
    tmp_path: Path,
) -> None:
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=("--test-child",),
        cancel_settle_seconds=0.5,
    )
    transport = _transport(config)
    await transport.start()
    try:
        assert await transport.open_session(resume=None) == "conv-acp"
        transport.prompt(
            transport.next_request_id(),
            [{"type": "text", "text": "marker\neof"}],
            before_write=lambda: None,
        )
        async with asyncio.timeout(3):
            while transport.returncode is None:
                await asyncio.sleep(0.005)
        assert transport.returncode == 0
        pid = int((tmp_path / "child.pid").read_text())
        probe = PsutilChildProbe()
        assert probe.is_alive(pid)
        await transport.close(broken=True)
        assert not probe.is_alive(pid)
    finally:
        await transport.close(broken=True)


@pytest.mark.parametrize(
    ("command", "prefix"),
    [("native-HIDE", "MEIVA_HIDE_FROM_USER"), ("native-DISPLAY", "MEIVA_DISPLAY_TO_USER")],
)
@pytest.mark.parametrize(
    ("extra_args", "cause"),
    [((), HarnessCause.SCHEDULED_WAKE), (("--test-native-child",), HarnessCause.BACKGROUND_TASK)],
)
async def test_native_task_reply_is_a_harness_turn_without_inbound(
    tmp_path: Path, command: str, prefix: str, extra_args: tuple[str, ...], cause: HarnessCause
) -> None:
    sink: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=sink.append, extra_args=extra_args)
    try:
        await harness.session.deliver([HarnessInput.of("foreground", command)])
        foreground = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        harness.release()
        begun = await harness.wait_for(
            lambda e: isinstance(e, TurnStarted) and e.origin is TurnOrigin.HARNESS
        )
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == begun.turn_id
        )
        assert done.turn_id != foreground.turn_id and begun.keys == ()
        assert begun.cause is cause
        assert done.text == f"{prefix}\nTIMER_PURPLE"
        assert done.usage == Usage(267, 3, 0, 30)
        assert not done.usage_uncertain
        assert len(harness.prompts()) == 1 and sink == []
    finally:
        await harness.stop()


async def test_load_discards_replay_and_sets_model_and_effort_before_prompt(tmp_path: Path) -> None:
    sink: list[UnsolicitedOutput] = []
    seed_replay_history(tmp_path)
    harness = await start(
        tmp_path,
        resume="conv-acp",
        unsolicited=sink.append,
        extra_args=("--test-late-load-replay",),
    )
    try:
        await harness.session.deliver([HarnessInput.of("new", "resumed")])
        completed = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        assert completed.text == "MEIVA_DISPLAY_TO_USER\nFresh reply"
        assert completed.usage == Usage(250, 3, 0, 30) and not completed.usage_uncertain
        assert [e.text for e in harness.events if isinstance(e, AssistantText)] == [completed.text]
        frames = harness.frames()
        assert [f["method"] for f in frames] == [
            "initialize",
            "session/load",
            "session/set_model",
            "session/prompt",
        ]
        assert frames[2]["params"] == {"sessionId": "conv-acp", "modelId": "gemini-4-hydrogen-low"}
        assert sink == []
    finally:
        await harness.stop()


async def test_subagent_completion_overlaps_a_prompt_without_using_its_usage(
    tmp_path: Path,
) -> None:
    sink: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=sink.append, extra_args=("--test-native-child",))
    try:
        await harness.session.deliver([HarnessInput.of("seed", "child-seed")])
        await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        await harness.session.deliver([HarnessInput.of("overlap", "child-overlap")])
        native = await harness.wait_for(
            lambda event: isinstance(event, TurnStarted) and event.origin is TurnOrigin.HARNESS
        )
        child = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id == native.turn_id
        )
        foreground = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.text == "FOREGROUND"
        )
        assert child.text == "CHILD CALLBACK" and child.usage == Usage(267, 3, 0, 30)
        assert foreground.usage == Usage(40, 5, 0, 30) and not foreground.usage_uncertain
        assert native.cause is HarnessCause.BACKGROUND_TASK and sink == []
    finally:
        await harness.stop()
