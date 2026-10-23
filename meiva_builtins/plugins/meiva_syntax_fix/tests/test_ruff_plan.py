import pytest

from meiva_builtins.plugins.meiva_syntax_fix._ruff_plan import (
    FailureKind,
    RuffFailure,
    RuffRun,
    fix_argv,
    format_argv,
    give_up_warning,
    rewritten_source,
    undefined_names,
    undefined_names_argv,
)

_RUFF = RuffRun(executable="/venv/bin/ruff")


def test_each_stage_runs_the_given_executable_with_the_source_on_stdin():
    assert fix_argv(_RUFF) == (
        "/venv/bin/ruff",
        "check",
        "--fix",
        "--stdin-filename",
        "script.py",
        "--quiet",
        "-",
    )
    assert format_argv(_RUFF) == (
        "/venv/bin/ruff",
        "format",
        "--stdin-filename",
        "script.py",
        "-",
    )
    assert undefined_names_argv(_RUFF) == (
        "/venv/bin/ruff",
        "check",
        "--select",
        "F821",
        "--output-format",
        "json",
        "--stdin-filename",
        "script.py",
        "-",
    )


def test_the_budget_defaults_to_the_shared_constant_and_can_be_shortened():
    assert RuffRun(executable="ruff").timeout_seconds == 5
    assert RuffRun(executable="ruff", timeout_seconds=0.2).timeout_seconds == 0.2


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, "fixed\n", "fixed\n"),
        (1, "fixed\n", "original\n"),
        (0, "", "original\n"),
        (0, "  \n", "original\n"),
    ],
)
def test_rewritten_source_takes_stdout_only_from_a_clean_exit_with_output(
    returncode: int, stdout: str, expected: str
):
    assert rewritten_source("original\n", returncode, stdout) == expected


_DIAGNOSTICS = (
    '[{"code":"F821","message":"Undefined name `json`"},'
    '{"code":null,"message":"SyntaxError: invalid"},'
    '{"code":"F401","message":"`os` imported but unused"},'
    '{"code":"F821","message":"Undefined name `Path`"}]'
)


def test_undefined_names_keeps_only_the_f821_names():
    assert undefined_names(1, _DIAGNOSTICS) == {"json", "Path"}


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(2, _DIAGNOSTICS), (0, ""), (1, "not json"), (1, "[]")],
)
def test_a_crash_empty_or_unreadable_output_names_nothing(returncode: int, stdout: str):
    assert undefined_names(returncode, stdout) == frozenset()


def test_a_timeout_names_the_budget_and_the_input_size():
    failure = RuffFailure(FailureKind.TIMEOUT, timeout=5)

    assert give_up_warning("format", 42, failure, "ruff") == (
        "ruff format did not finish within 5s on 42-char source — passing through unchanged"
    )


def test_a_missing_executable_names_it():
    assert give_up_warning("format", 42, RuffFailure(FailureKind.MISSING), "/x/ruff") == (
        "ruff executable '/x/ruff' not found — syntax-fix "
        "ruff stage skipped (source passed through unchanged)"
    )


def test_a_codec_failure_names_the_error():
    failure = RuffFailure(
        FailureKind.UNICODE, error_type="UnicodeEncodeError", error_text="cannot encode"
    )

    assert give_up_warning("check --fix", 3, failure, "ruff") == (
        "ruff check --fix failed with UnicodeEncodeError (cannot encode) "
        "— passing through unchanged"
    )


def test_any_other_os_error_names_its_errno():
    failure = RuffFailure(FailureKind.OS_ERROR, errno=24, strerror="Too many open files")

    assert give_up_warning("format", 3, failure, "ruff") == (
        "ruff format failed with OSError errno=24 (Too many open files) — passing through unchanged"
    )
