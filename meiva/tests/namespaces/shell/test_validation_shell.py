"""Argument validation of the `meiva.shell` entry points."""

from __future__ import annotations

from pathlib import Path

import pytest

from meiva import shell as _shell

pytestmark = pytest.mark.usefixtures("sdk_model_owner", "pty_service", "_isolated_agent")


class TestShellEntries:
    def test_run_cmd_unwraps(self) -> None:
        result = _shell.run(("echo sdk-validation-smoke",))  # pyright: ignore[reportArgumentType]
        assert "sdk-validation-smoke" in str(result)

    def test_run_multi_element_cmd_type_errors(self) -> None:
        with pytest.raises(TypeError, match="cmd must be a string"):
            _shell.run(("echo a", "echo b"))  # pyright: ignore[reportArgumentType]

    def test_run_background_normalizes_and_starts(self, _agent_row: int) -> None:
        run = _shell.run_background(("echo hi",), name=("bg",), keep=True, ttl=60)  # pyright: ignore[reportArgumentType]
        try:
            assert Path(run.output_path).name == f"{run.session_id}_bg.log"
            assert _shell.sessions.list()[run.session_id] == "bg"
        finally:
            _shell.sessions.kill(run)

    def test_run_background_multi_element_name_type_errors(self) -> None:
        with pytest.raises(TypeError, match="name must be a string"):
            _shell.run_background("echo hi", name=("a", "b"), ttl=60)  # pyright: ignore[reportArgumentType]
