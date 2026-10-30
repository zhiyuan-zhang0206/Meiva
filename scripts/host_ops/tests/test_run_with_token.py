# meiva:note
# Unit tests for the run-with-token entry: the env it builds and the argv it refuses.
from __future__ import annotations

from pathlib import Path

from scripts.host_ops.run_with_token import TOKEN_ENV, command_from, main, token_env


# meiva:note
# The token is read at call time, so an inherited value from an older generation must lose.
def test_token_env_overwrites_a_stale_value() -> None:
    child = token_env({"MEIVA_API_TOKEN": "stale", "PATH": "/bin"}, "fresh")
    assert child[TOKEN_ENV] == "fresh"
    assert child["PATH"] == "/bin"


def test_token_env_leaves_the_parent_mapping_untouched() -> None:
    parent = {"PATH": "/bin"}
    token_env(parent, "fresh")
    assert parent == {"PATH": "/bin"}


# meiva:note
# The command lives after the literal `--`: a flag-looking argument belongs to the child, not to
# this entry, so parsing stops at the separator instead of guessing.
def test_command_from_takes_everything_after_the_separator() -> None:
    assert command_from(["--check", "--", "curl", "-s", "http://x"]) == ["curl", "-s", "http://x"]


def test_command_from_is_empty_without_a_separator() -> None:
    assert command_from(["curl", "http://x"]) == []


# meiva:note
# No separator is a usage error, not an empty command: running nothing must never look like a
# successful no-op.
def test_main_refuses_a_missing_separator() -> None:
    assert main(["curl", "http://x"]) == 2


# meiva:note
# A home with no installed capability answers False and exits non-zero - loud, because a caller
# that proceeds without a token would silently call the API unauthenticated.
def test_check_reports_absent_token_for_a_bare_home(tmp_path: Path) -> None:
    assert main(["--check", "--home", str(tmp_path)]) == 1
