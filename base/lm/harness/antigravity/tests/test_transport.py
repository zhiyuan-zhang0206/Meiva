from __future__ import annotations

import json
import sys
from pathlib import Path

from base.clock.tests.fakes import system_clock
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.transport import AgyTransport

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
