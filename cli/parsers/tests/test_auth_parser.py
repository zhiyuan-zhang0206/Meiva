# meiva:note
# `meiva auth set-password` states where the password comes from, and never takes it from argv.

from __future__ import annotations

import pytest

from cli.parsers import command_options, parse_args


@pytest.mark.parametrize(("flag", "other"), [("--prompt", "stdin"), ("--stdin", "prompt")])
def test_one_named_source_is_accepted_and_the_other_is_off(flag: str, other: str) -> None:
    args = parse_args(["auth", "set-password", flag])
    sources = {"prompt": args.prompt, "stdin": args.stdin}
    assert sources[flag.removeprefix("--")] is True
    assert sources[other] is False


def test_no_source_is_an_error_that_lists_both_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_args(["auth", "set-password"])
    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert "--prompt" in err and "--stdin" in err and "required" in err


def test_both_sources_are_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_args(["auth", "set-password", "--prompt", "--stdin"])
    assert refused.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_a_password_in_argv_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_args(["auth", "set-password", "--prompt", "hunter2-hunter2"])
    assert refused.value.code == 2
    assert "unrecognized arguments: hunter2-hunter2" in capsys.readouterr().err


def test_the_verb_takes_only_its_two_sources() -> None:
    assert command_options()[("meiva", "auth", "set-password")] == {"--help", "--prompt", "--stdin"}
