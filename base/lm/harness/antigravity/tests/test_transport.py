from __future__ import annotations

import json
import sys
from pathlib import Path

from base.clock.tests.fakes import system_clock
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.timing_store import timing_path
from base.lm.harness.antigravity.transcript.file import AntigravityRecordFile
from base.lm.harness.antigravity.transcript.window import EntryParser
from base.lm.harness.antigravity.transport import AgyTransport
from base.lm.harness.session.transcript.entries import Reasoning

FAKE_CLI = Path(__file__).with_name("fake_agy.py")


def test_the_command_line_pins_the_stream_protocol(tmp_path: Path) -> None:
    argv = AntigravityConfig(cwd=tmp_path).command(resume=None)
    assert argv[:1] == ("agy",)
    assert "--input-format" in argv and "stream-json" in argv
    assert "--output-format" in argv
    assert argv[argv.index("--model") + 1] == "gemini-4-argon-high"
    assert "--conversation" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_resume_and_skip_permissions_are_passed_through(tmp_path: Path) -> None:
    argv = AntigravityConfig(
        cwd=tmp_path, skip_permissions=True, executable=("fake", "--flag")
    ).command(resume="conv-9")
    assert argv[:2] == ("fake", "--flag")
    index = argv.index("--conversation")
    assert argv[index + 1] == "conv-9"
    assert "--dangerously-skip-permissions" in argv


async def test_pump_stdout_handles_frame_above_eight_mib_and_trailing_eof(
    tmp_path: Path,
) -> None:
    payload_bytes = 10 * 1024 * 1024
    trailing_bytes = 128 * 1024
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "conversation_id": "conv-big",
                "raw_stream": {
                    "blob_bytes": payload_bytes,
                    "trailing_eof_bytes": trailing_bytes,
                },
            }
        ),
        encoding="utf-8",
    )
    config = AntigravityConfig(cwd=tmp_path, executable=(sys.executable, str(FAKE_CLI)))
    transport = AgyTransport(
        config,
        environ={"FAKE_AGY_PLAN": str(plan_file)},
        clock=system_clock(),
    )
    await transport.start(resume=None)
    try:
        seen = [event async for event in transport.events()]
        assert [event["event"] for event in seen] == ["init", "step_update", "result"]
        assert transport.conversation_id == "conv-big"
        blob = seen[1]["blob"]
        assert isinstance(blob, str) and len(blob) == payload_bytes
        tail_blob = seen[2]["blob"]
        assert isinstance(tail_blob, str) and len(tail_blob) == trailing_bytes
        assert transport.stderr_tail == ()
    finally:
        await transport.close()


async def test_pump_stdout_records_limit_error_when_frame_exceeds_max_frame_bytes(
    tmp_path: Path,
) -> None:
    plan_file = tmp_path / "plan-overflow.json"
    plan_file.write_text(
        json.dumps({"conversation_id": "conv-cap", "raw_stream": {"blob_bytes": 16384}}),
        encoding="utf-8",
    )
    config = AntigravityConfig(cwd=tmp_path, executable=(sys.executable, str(FAKE_CLI)))
    transport = AgyTransport(
        config,
        environ={"FAKE_AGY_PLAN": str(plan_file)},
        clock=system_clock(),
        max_frame_bytes=4096,
    )
    await transport.start(resume=None)
    try:
        seen = [event async for event in transport.events()]
        assert [event["event"] for event in seen] == ["init"]
        assert "stdout line exceeded the reader limit" in transport.stderr_tail
    finally:
        await transport.close()


async def test_existing_trajectory_rpc_collects_native_timing_without_another_rpc(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"conversation_id": "conv-timing", "turns": []}))
    path = (
        tmp_path
        / ".gemini/antigravity-cli/brain/conv-timing/.system_generated/logs/transcript_full.jsonl"
    )
    path.parent.mkdir(parents=True)
    raw = (
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "step_index": 0,
                "created_at": "2026-11-17T05:30:00Z",
                "thinking": "synthetic thought",
            }
        )
        + "\n"
    )
    path.write_text(raw)
    calls = tmp_path / "calls.jsonl"
    transport = AgyTransport(
        AntigravityConfig(cwd=tmp_path, executable=(sys.executable, str(FAKE_CLI))),
        environ={
            "HOME": str(tmp_path),
            "FAKE_AGY_PLAN": str(plan),
            "FAKE_AGY_TRAJECTORY_CALLS": str(calls),
            "FAKE_AGY_TRAJECTORY_FIXTURE": str(
                Path(__file__).with_name("fixtures") / "native_thinking_timing.json"
            ),
        },
        clock=system_clock(),
    )
    await transport.start(resume="conv-timing")
    try:
        measured = await transport.trajectory()
        assert measured.steps[0].thinking_ms == 30265
        assert timing_path(path).is_file()
        assert len(calls.read_text().splitlines()) == 1
        entries = EntryParser().entries(AntigravityRecordFile(path).rows_after(0).rows)
        assert isinstance(entries[0], Reasoning) and entries[0].duration_ms == 30265
        assert path.read_text() == raw
    finally:
        await transport.close()
