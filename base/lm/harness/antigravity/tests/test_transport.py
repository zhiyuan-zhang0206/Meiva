from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.config import AntigravityConfig


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
