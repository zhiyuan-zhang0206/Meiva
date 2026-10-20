from __future__ import annotations

import pytest

from cli.parsers import build_parser


@pytest.mark.parametrize(
    "command",
    [
        ["start", "--all-services"],
        ["start", "--only-service", "gateway"],
        [
            "stop",
            "--yes",
            "--close-shell-sessions",
            "--stop-browser",
            "--stop-extras",
            "--keep-no-services",
            "--stop-infra",
            "--mode",
            "smooth",
            "--timeout",
            "300",
        ],
        ["restart", "--only", "agent-host", "--mode", "smooth", "--timeout", "120"],
    ],
)
def test_hint_commands_parse(command: list[str]) -> None:
    build_parser().parse_args(command)


def test_bare_start_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["start"])
    assert raised.value.code == 2
    assert "--all-services" in capsys.readouterr().err
