from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from base.clock import SYSTEM_TIMERS
from base.clock.tests.fakes import clock_over, system_clock
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.mapping import transcript_path
from base.lm.harness.antigravity.tests.fake_agy import declare_child
from base.lm.harness.antigravity.tests.support import Harness, seed_replay_history, start
from base.lm.harness.antigravity.transport import MAX_FRAME_BYTES, AgyTransport
from base.lm.harness.delivery import HarnessInput
from base.lm.harness.events import (
    AssistantText,
    HarnessCause,
    SessionFailed,
    ToolResult,
    ToolUse,
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


@pytest.mark.parametrize(
    ("command", "usage"),
    [
        ("child-overlap", Usage(40, 5, 0, 30)),
        ("child-no-usage", Usage(40, 3, 0, 30)),
        ("child-no-usage-incomplete", Usage(40, 0, 0, 30)),
        ("child-late", Usage(40, 5, 0, 30)),
        ("child-deferred", Usage(40, 5, 0, 30)),
    ],
)
async def test_subagent_completion_overlaps_a_prompt_without_using_its_usage(
    tmp_path: Path, command: str, usage: Usage
) -> None:
    sink: list[UnsolicitedOutput] = []
    harness = await start(
        tmp_path, unsolicited=sink.append, extra_args=("--test-native-child",), reply_seconds=1
    )
    try:
        await harness.session.deliver([HarnessInput.of("seed", "child-seed")])
        seed = await harness.wait_for_type(TurnCompleted)
        await harness.session.deliver([HarnessInput.of("overlap", command)])
        foreground = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.text == "FOREGROUND"
        )
        if command == "child-deferred":
            harness.release()
        native = await harness.wait_for(
            lambda event: isinstance(event, TurnStarted) and event.origin is TurnOrigin.HARNESS
        )
        child = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id == native.turn_id
        )
        assert child.text == "CHILD CALLBACK" and child.usage == Usage(267, 3, 0, 30)
        assert foreground.usage == usage
        assert foreground.usage_uncertain == command.endswith("-incomplete")
        assert native.cause is HarnessCause.BACKGROUND_TASK and sink == []
        if command == "child-late":
            harness.release()
            await harness.wait_for_marker("child-frame-sent")
            await harness.session.deliver([HarnessInput.of("next", "next")])
            await harness.wait_for(
                lambda event: (
                    isinstance(event, TurnCompleted)
                    and event.turn_id not in {seed.turn_id, foreground.turn_id, child.turn_id}
                )
            )
            assert (
                sum(
                    isinstance(event, TurnCompleted) and event.text == "CHILD CALLBACK"
                    for event in harness.events
                )
                == 1
            )
    finally:
        await harness.stop()


@pytest.mark.parametrize("command", ["textless", "textless-no-planner-usage"])
async def test_resumed_textless_turn_completes_despite_late_replay(
    tmp_path: Path, command: str
) -> None:
    seed_replay_history(tmp_path)
    sink: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, resume="conv-acp", unsolicited=sink.append, reply_seconds=1)
    try:
        await harness.session.deliver([HarnessInput.of("textless", command)])
        done = await harness.wait_for_type(TurnCompleted)
        assert not done.text and done.usage == Usage(10) and not done.usage_uncertain
        assert not any(isinstance(event, AssistantText) for event in harness.events)
        assert sink == []
    finally:
        await harness.stop()


async def test_resumed_cancel_keeps_proven_partial_text_and_usage(tmp_path: Path) -> None:
    seed_replay_history(tmp_path)
    sink: list[UnsolicitedOutput] = []
    harness = await start(
        tmp_path, resume="conv-acp", unsolicited=sink.append, extra_args=("--test-cancel-partial",)
    )
    try:
        await harness.session.deliver([HarnessInput.of("cancel", "hold")])
        tool = await harness.wait_for_type(ToolUse)
        cancel = await harness.session.interrupt(turn_id=tool.turn_id)
        done = await harness.wait_for_type(TurnCompleted)
        assert cancel.settled and done.interrupted
        assert done.text == "VISIBLE PARTIAL" and done.usage == Usage(80, 6, 0, 60)
        pid = harness.session.cli_pid
        await harness.session.deliver([HarnessInput.of("next", "after-cancel")])
        again = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id != done.turn_id
        )
        assert again.text == "OK" and harness.session.cli_pid == pid
        assert sink == []
    finally:
        await harness.stop()


def _assistant_texts(harness: Harness, turn_id: str) -> list[str]:
    return [
        event.text
        for event in harness.events
        if isinstance(event, AssistantText) and event.turn_id == turn_id
    ]


@pytest.mark.parametrize(
    ("command", "text", "uncertain", "tool_count", "steps", "native_timing"),
    [
        (
            "resumed-command-completion",
            "MEIVA_DISPLAY_TO_USER\nFresh multi-step reply",
            False,
            10,
            11,
            "none",
        ),
        ("unresolved-attribution", "", True, 6, 6, "after-marker"),
        ("unresolved-attribution", "", True, 6, 6, "none"),
        ("unresolved-attribution", "", True, 6, 6, "before-marker"),
        ("unresolved-attribution", "", True, 6, 6, "before-marker-with-tool"),
        ("unresolved-attribution", "", True, 6, 6, "tool-after-marker"),
        ("unresolved-attribution", "", True, 6, 6, "delayed-planner"),
        ("unresolved-attribution", "", True, 6, 6, "delayed-planner-early"),
        ("unresolved-attribution", "", True, 6, 6, "delayed-planner-interleaved"),
        ("unresolved-attribution", "", True, 6, 6, "delayed-planner-trailing"),
        ("unresolved-attribution", "", True, 6, 6, "foreground-preamble"),
    ],
)
async def test_resumed_multi_step_command_completion(
    tmp_path: Path,
    command: str,
    text: str,
    uncertain: bool,
    tool_count: int,
    steps: int,
    native_timing: str,
) -> None:
    seed_replay_history(tmp_path)
    sink: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, resume="conv-acp", unsolicited=sink.append, reply_seconds=0.2)
    try:
        await harness.session.deliver([HarnessInput.of("first", command)])
        done = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted | SessionFailed)
        )
        assert isinstance(done, TurnCompleted), done
        assert not done.is_error and done.stop_reason == "end_turn"
        assert done.text == text and done.usage_uncertain == uncertain and done.num_turns == steps
        assert [e.text for e in harness.events if isinstance(e, AssistantText)] == (
            [text] if text else []
        )
        assert sum(isinstance(e, ToolUse) for e in harness.events) == tool_count

        await _assert_reused_prompt(harness, done.turn_id, native_timing=native_timing)
        assert sink == []
    finally:
        await harness.stop()


async def _release_native_before_followup(harness: Harness, native_timing: str) -> None:
    if native_timing.startswith("before-marker"):
        if native_timing.endswith("-with-tool"):
            harness.release("include-native-tool")
            harness.release("delay-native-frame")
        harness.release("release-native")
        await harness.wait_for_marker("native-written")
        if native_timing == "before-marker":
            await harness.wait_for_marker("collision-receipt-read")


async def _assert_reused_prompt(
    harness: Harness, first_turn_id: str, *, native_timing: str
) -> None:
    pid, generation = harness.session.cli_pid, harness.session.generation
    await _release_native_before_followup(harness, native_timing)
    followup = {
        "after-marker": "late-native",
        "before-marker-with-tool": "next-with-delayed-native-frame",
        "tool-after-marker": "tool-then-foreground",
        "delayed-planner": "delayed-planner",
        "delayed-planner-early": "delayed-planner-early",
        "delayed-planner-interleaved": "delayed-planner-interleaved",
        "delayed-planner-trailing": "delayed-planner-trailing",
        "foreground-preamble": "foreground-preamble",
    }.get(native_timing, "next")
    await harness.session.deliver([HarnessInput.of("second", followup)])
    second = await harness.wait_for(
        lambda event: (
            isinstance(event, TurnStarted)
            and event.origin is TurnOrigin.INPUT
            and event.turn_id != first_turn_id
        )
    )
    if native_timing.startswith("delayed-planner"):
        await harness.wait_for_marker("collision-receipt-read")
        harness.release("release-foreground")
    again = await harness.wait_for(
        lambda event: isinstance(event, TurnCompleted) and event.turn_id == second.turn_id
    )
    expected = ["Working", "OK"] if native_timing == "foreground-preamble" else ["OK"]
    assert again.text == "\n".join(expected)
    assert not again.usage_uncertain
    fenced = native_timing == "before-marker-with-tool"
    assert (harness.session.generation > generation) is fenced
    assert (harness.session.cli_pid != pid) is fenced
    assert _assistant_texts(harness, second.turn_id) == expected
    if native_timing not in {"none", "foreground-preamble"}:
        if native_timing == "tool-after-marker":
            harness.release("release-native")
        await _assert_native_reply(harness, with_tool="tool" in native_timing)
    else:
        assert [event.origin for event in harness.events if isinstance(event, TurnStarted)] == [
            TurnOrigin.INPUT,
            TurnOrigin.INPUT,
        ]


class AttributionTimers(ManualTimers):
    async def asleep(self, seconds: float) -> None:
        await SYSTEM_TIMERS.asleep(seconds)


@pytest.mark.parametrize(
    ("case", "expires"),
    [("text", False), ("text", True), ("tool", False)],
    ids=["text", "deadline", "tool"],
)
async def test_undrained_native_echo_preserves_followup(
    tmp_path: Path, case: str, expires: bool
) -> None:
    seed_replay_history(tmp_path)
    timers = AttributionTimers()
    harness = await start(
        tmp_path,
        resume="conv-acp",
        reply_seconds=1,
        clock=clock_over(timers),
        extra_args=(f"--test-native-same-{case}", "--test-late-load-replay"),
    )
    try:
        await harness.session.deliver([HarnessInput.of("first", "unresolved-attribution")])
        await harness.wait_for_marker("unresolved-stop-read")
        timers.advance(2)
        first = await harness.wait_for_type(TurnCompleted)
        harness.release("delay-native-frame")
        harness.release("release-native")
        await harness.wait_for_marker("native-written")
        native = await harness.wait_for(
            lambda event: isinstance(event, TurnStarted) and event.origin is TurnOrigin.HARNESS
        )
        pid, generation = harness.session.cli_pid, harness.session.generation
        followup = "same-text-without-planner" if case == "text" else "same-tool"
        await harness.session.deliver([HarnessInput.of("second", followup)])
        if case == "text":
            await harness.wait_for_marker("foreground-stop-read")
            assert not any(
                isinstance(e, AssistantText) and e.turn_id not in {first.turn_id, native.turn_id}
                for e in harness.events
            )
            if expires:
                timers.advance(2)
            else:
                harness.release("write-foreground")
        done = await harness.wait_for(
            lambda event: (
                isinstance(event, TurnCompleted)
                and event.turn_id not in {first.turn_id, native.turn_id}
            )
        )
        assert harness.session.cli_pid != pid and done.generation > generation
        assert sum(f.get("method") == "session/load" for f in harness.frames()) == 2
        expected = "" if expires else "OK"
        assert done.text == expected and done.usage_uncertain is expires
        assert _assistant_texts(harness, done.turn_id) == ([expected] if expected else [])
        if case == "tool":
            assert [
                type(e)
                for e in harness.events
                if isinstance(e, (ToolUse, ToolResult)) and e.turn_id == done.turn_id
            ] == [ToolUse, ToolResult]
    finally:
        await harness.stop()


@pytest.mark.parametrize(
    ("command", "text", "usage"),
    [
        ("resumed", "MEIVA_DISPLAY_TO_USER\nFresh reply", Usage(250, 3, 0, 30)),
        ("fresh-textless:end_turn", "", Usage(40, 0, 0, 30)),
    ],
    ids=["history", "textless"],
)
async def test_fenced_reload_projects_first_prompt(
    tmp_path: Path, command: str, text: str, usage: Usage
) -> None:
    seed_replay_history(tmp_path)
    harness = await start(
        tmp_path, resume="conv-acp", reply_seconds=0.2, extra_args=("--test-late-load-replay",)
    )
    try:
        await harness.session.deliver([HarnessInput.of("first", "unresolved-attribution")])
        await harness.wait_for_type(TurnCompleted)
        harness.release("delay-native-frame")
        harness.release("release-native")
        await _assert_native_reply(harness, with_tool=False)
        generation, pid = harness.session.generation, harness.session.cli_pid
        await harness.session.deliver([HarnessInput.of("second", command)])
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.generation > generation
        )
        assert harness.session.cli_pid != pid
        assert done.text == text and done.usage == usage and not done.usage_uncertain
        assert _assistant_texts(harness, done.turn_id) == ([text] if text else [])
    finally:
        await harness.stop()


@pytest.mark.parametrize("case", ["child", "drained", "recorded"])
async def test_early_native_output_survives_idle_snapshot(tmp_path: Path, case: str) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.deliver([HarnessInput.of("first", f"prepare-idle-{case}")])
        first = await harness.wait_for_type(TurnCompleted)
        pid = harness.session.cli_pid
        harness.release("send-early-idle")
        await harness.wait_for_marker("collision-receipt-read")
        harness.release("finish-early-idle")
        native = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.text == "NATIVE RESPONSE"
        )
        await harness.session.deliver([HarnessInput.of("second", "next")])
        second = await harness.wait_for(
            lambda e: (
                isinstance(e, TurnCompleted)
                and e.text == "OK"
                and e.turn_id not in {first.turn_id, native.turn_id}
            )
        )
        needs_fence = case != "recorded"
        assert (harness.session.cli_pid != pid) is needs_fence
        assert (second.generation > first.generation) is needs_fence
        assert _assistant_texts(harness, second.turn_id) == ["OK"]
        assert [e.text for e in harness.events if isinstance(e, TurnCompleted)] == [
            "OK",
            "NATIVE RESPONSE",
            "OK",
        ]
    finally:
        await harness.stop()


async def _send_input(harness: Harness, key: str, command: str) -> TurnCompleted:
    await harness.session.deliver([HarnessInput.of(key, command)])
    turn: object = await harness.wait_for(
        lambda e: isinstance(e, TurnStarted) and e.origin is TurnOrigin.INPUT and key in e.keys
    )
    assert isinstance(turn, TurnStarted)
    done: object = await harness.wait_for(
        lambda e: isinstance(e, TurnCompleted) and e.turn_id == turn.turn_id
    )
    assert isinstance(done, TurnCompleted)
    return done


async def _native_completion(harness: Harness, text: str) -> TurnCompleted:
    done: object = await harness.wait_for(
        lambda e: (
            isinstance(e, TurnCompleted)
            and e.text == text
            and any(
                isinstance(started, TurnStarted)
                and started.turn_id == e.turn_id
                and started.origin is TurnOrigin.HARNESS
                for started in harness.events
            )
        )
    )
    assert isinstance(done, TurnCompleted)
    return done


async def test_unmatched_idle_echo_cannot_discharge_a_later_native_reply(tmp_path: Path) -> None:
    harness = await start(tmp_path, reply_seconds=0.2)
    try:
        first = await _send_input(harness, "first", "prepare-idle-stale")
        assert first.text == "READY"
        pid = harness.session.cli_pid
        harness.release("send-early-idle")
        await harness.wait_for_marker("collision-receipt-read")
        harness.release("finish-early-idle")
        native = await _native_completion(harness, "READY")
        assert native.generation == first.generation
        following = await _send_input(harness, "next", "next-with-idle-echo")
        assert following.text == "OK"
        assert _assistant_texts(harness, following.turn_id) == ["OK"]
        assert following.generation > first.generation and harness.session.cli_pid != pid
    finally:
        await harness.stop()


@pytest.mark.parametrize("interruption", ["adjacent", "intervening"])
async def test_other_completed_work_does_not_delete_unknown_child_notice(
    tmp_path: Path, interruption: str
) -> None:
    harness = await start(tmp_path, reply_seconds=0.2)
    try:
        first = await _send_input(harness, "first", f"prepare-idle-{interruption}")
        harness.release("send-early-idle")
        await harness.wait_for_marker("collision-receipt-read")
        if interruption == "adjacent":
            await _native_completion(harness, "TIMER RESPONSE")
        else:
            bridge = await _send_input(harness, "bridge", "idle-intervening")
            assert bridge.text == "OK" and bridge.generation == first.generation
            await harness.wait_for_marker("foreground-stop-read")
        harness.release("finish-early-idle")
        child = await _native_completion(harness, "CHILD RESPONSE")
        assert child.generation == first.generation
        following = await _send_input(harness, "next", "next")
        assert following.text == "OK" and following.generation == first.generation
        assert [
            e.text
            for e in harness.events
            if isinstance(e, AssistantText) and e.text in {"CHILD RESPONSE", "TIMER RESPONSE"}
        ] == (
            ["TIMER RESPONSE", "CHILD RESPONSE"]
            if interruption == "adjacent"
            else ["CHILD RESPONSE"]
        )
    finally:
        await harness.stop()


async def test_pending_completed_child_survives_generation_fence(tmp_path: Path) -> None:
    harness = await start(tmp_path, reply_seconds=0.2)
    try:
        first = await _send_input(harness, "first", "prepare-idle-fence")
        pid = harness.session.cli_pid
        harness.release("send-early-idle")
        await harness.wait_for_marker("collision-receipt-read")
        timer = await _native_completion(harness, "TIMER RESPONSE")
        assert timer.generation == first.generation

        following = await _send_input(harness, "second", "next")
        assert following.text == "OK" and following.generation > first.generation
        new_pid = harness.session.cli_pid
        assert new_pid is not None and new_pid != pid

        transcript = transcript_path("conv-acp", home=harness.home)
        assert transcript is not None
        _ = declare_child(transcript)
        child = await _native_completion(harness, "CHILD RESPONSE")
        last = await _send_input(harness, "third", "next")
        assert last.text == "OK" and last.generation == child.generation == following.generation
        assert harness.session.cli_pid == new_pid
        assert [
            e.text
            for e in harness.events
            if isinstance(e, TurnCompleted) and e.text in {"TIMER RESPONSE", "CHILD RESPONSE"}
        ] == ["TIMER RESPONSE", "CHILD RESPONSE"]
    finally:
        await harness.stop()


async def _assert_native_reply(harness: Harness, *, with_tool: bool) -> None:
    native = await harness.wait_for(
        lambda event: isinstance(event, TurnStarted) and event.origin is TurnOrigin.HARNESS
    )
    assert native.cause is HarnessCause.BACKGROUND_TASK
    assert _assistant_texts(harness, native.turn_id) == ["LATE NATIVE REPLY"]
    if with_tool:
        completed = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id == native.turn_id
        )
        assert completed.steps == (Usage(155, 3, 0, 30), Usage(267, 3, 0, 30))
        assert completed.num_turns == 2 and completed.usage == Usage(422, 6, 0, 60)
        assert completed.usage_uncertain


@pytest.mark.parametrize("order", ["native-first", "foreground-first"])
async def test_equal_usage_leaves_both_replies_unassigned(tmp_path: Path, order: str) -> None:
    seed_replay_history(tmp_path)
    sink: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, resume="conv-acp", unsolicited=sink.append, reply_seconds=0.2)
    try:
        await harness.session.deliver([HarnessInput.of("first", "unresolved-attribution")])
        first = await harness.wait_for_type(TurnCompleted)
        assert first.text == "" and first.usage_uncertain
        pid = harness.session.cli_pid
        await harness.session.deliver([HarnessInput.of("second", f"usage-tie-{order}")])
        second = await harness.wait_for(
            lambda event: isinstance(event, TurnCompleted) and event.turn_id != first.turn_id
        )
        assert second.text == "" and second.usage_uncertain
        assert second.stop_reason == "end_turn" and not second.is_error
        assert [e.origin for e in harness.events if isinstance(e, TurnStarted)] == [
            TurnOrigin.INPUT,
            TurnOrigin.INPUT,
        ]
        assert not any(isinstance(e, AssistantText) for e in harness.events)
        assert sink == [] and harness.session.cli_pid == pid
    finally:
        await harness.stop()
