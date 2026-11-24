from __future__ import annotations

import pytest

from cli.parsers import build_parser


def test_observability_render_parser_flags() -> None:
    args = build_parser().parse_args(["observability", "render", "--force", "--repo-only"])
    assert args.observability_cmd == "render"
    assert args.force is True
    assert args.repo_only is True
    assert args.func.__name__ == "_h_observability"


@pytest.mark.parametrize(
    "verb", [["on", "--all-services"], ["off", "--all-services"], ["status"], ["render"]]
)
def test_obs_is_the_same_command_as_observability(verb: list[str]) -> None:
    long = build_parser().parse_args(["observability", *verb])
    short = build_parser().parse_args(["obs", *verb])
    assert short.observability_cmd == long.observability_cmd == verb[0]
    assert short.func.__name__ == long.func.__name__ == "_h_observability"


def test_the_retired_lgtm_verb_is_not_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["lgtm", "status"])
    assert exit_info.value.code == 2
    assert "invalid choice: 'lgtm'" in capsys.readouterr().err
