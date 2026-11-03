# meiva:note
# The daily PR-flow job starts `pr_flow_export.py` as a script, so the command line is the first
# thing that runs. The parser used to take its description from a module docstring the file does not
# have, which raised `IndexError` before a single argument was read.

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pytest

from scripts.ci.pull_requests import pr_flow_export


def _no_process(
    argv: Sequence[str], /, *, timeout: float, capture_output: Literal[True], text: Literal[True]
) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"the usage text must not start a process: {argv}")


def test_the_entry_builds_its_parser_and_prints_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        pr_flow_export.main(["--help"], run_process=_no_process, home=tmp_path)

    assert stopped.value.code == 0
    printed = capsys.readouterr().out
    assert "--window-days" in printed
    assert "OTLP gauges" in printed
