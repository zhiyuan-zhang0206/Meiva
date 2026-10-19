from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from cli.commands.harness import cmd_harness_exec
from cli.parsers import build_parser


class RecordingAttachment:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self) -> RecordingAttachment:
        self.entered += 1
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.exited += 1
        return False


def opener(attachment: RecordingAttachment, calls: list[tuple[Any, ...]]) -> Any:
    def open_attachment(handle: str | None, **kwargs: Any) -> RecordingAttachment:
        calls.append((handle, kwargs))
        return attachment

    return open_attachment


def test_exec_runs_the_file_inside_one_attachment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran.txt"
    script = tmp_path / "script.py"
    script.write_text(
        "import pathlib\nimport meiva\n"
        f"pathlib.Path(r'{marker}').write_text('ran')\n"
        "assert meiva is not None\n",
        encoding="utf-8",
    )
    attachment = RecordingAttachment()
    calls: list[tuple[Any, ...]] = []
    code = cmd_harness_exec(
        file=str(script),
        handle="hx_test",
        agent_id=41,
        session_id="session-1",
        opener=opener(attachment, calls),
    )
    assert code == 0
    assert marker.read_text(encoding="utf-8") == "ran"
    assert calls == [("hx_test", {"agent_id": 41, "session_id": "session-1"})]
    assert (attachment.entered, attachment.exited) == (1, 1)
    assert capsys.readouterr().err == ""


def test_exec_reads_stdin_when_no_file_is_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    attachment = RecordingAttachment()
    calls: list[tuple[Any, ...]] = []
    code = cmd_harness_exec(
        file="-",
        handle=None,
        agent_id=None,
        session_id=None,
        opener=opener(attachment, calls),
        stdin=io.StringIO("print('from stdin')\n"),
    )
    assert code == 0
    assert capsys.readouterr().out == "from stdin\n"
    assert calls == [(None, {"agent_id": None, "session_id": None})]


def test_an_empty_script_exits_one_with_a_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Any, ...]] = []
    code = cmd_harness_exec(
        file=None,
        handle=None,
        agent_id=None,
        session_id=None,
        opener=opener(RecordingAttachment(), calls),
        stdin=io.StringIO("   \n"),
    )
    assert code == 1
    assert "must be nonempty" in capsys.readouterr().err
    assert calls == []


def test_a_missing_file_exits_one_with_a_message(capsys: pytest.CaptureFixture[str]) -> None:
    code = cmd_harness_exec(
        file="/nonexistent/harness-script.py",
        handle=None,
        agent_id=None,
        session_id=None,
        opener=lambda *_args, **_kwargs: RecordingAttachment(),
    )
    assert code == 1
    assert "harness:" in capsys.readouterr().err


def test_a_refused_attachment_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    def refuse(*args: Any, **kwargs: Any) -> RecordingAttachment:
        raise RuntimeError("this process already has an external attachment")

    code = cmd_harness_exec(
        file="-",
        stdin=io.StringIO("print('never runs')\n"),
        handle=None,
        agent_id=None,
        session_id=None,
        opener=refuse,
    )
    assert code == 1
    assert "already has an external attachment" in capsys.readouterr().err


def test_the_parser_binds_the_harness_group() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["harness", "exec", "--file", "script.py", "--agent-id", "41", "--session-id", "s-1"]
    )
    assert args.cmd == "harness"
    assert args.harness_cmd == "exec"
    assert args.file == "script.py"
    assert args.agent_id == 41
    assert args.session_id == "s-1"
    assert args.func is not None


def test_the_parser_defaults_to_the_environment_handle() -> None:
    parser = build_parser()
    args = parser.parse_args(["harness", "exec"])
    assert args.handle is None
    assert args.file is None
