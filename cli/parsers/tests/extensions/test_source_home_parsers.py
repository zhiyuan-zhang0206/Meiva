# meiva:note
# A package source a person types: the parser places a leading `~` against the home this invocation
# read, and refuses a `~someone/...` spelling instead of resolving it through the password database
# (the rule `base.paths.expand.expand_home` owns).

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime

_VERBS = ("mcp install", "skill install", "skill scan")


# meiva:note
# Every verb that takes a package source answers a `~account` spelling the same way: a line on
# stderr and exit 1, never an expansion through the password database.
@pytest.mark.parametrize("verb", _VERBS)
def test_another_users_home_in_the_source_is_refused(
    verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.parsers import build_parser

    args = build_parser().parse_args([*verb.split(), "~someone/src"])
    args.runtime = SimpleNamespace(session=SimpleNamespace(user_home=Path("/home/operator")))

    assert args.func(args) == 1
    assert "names another user's home" in capsys.readouterr().err


# meiva:note
# `meiva skill scan ~/scan-target` scans the session home's directory and names it: the parser
# placed the `~`, so the command held a path it could open.
def test_the_scan_verb_finds_a_tilde_target_in_the_home_the_session_read(
    command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.parsers import build_parser

    target = command_runtime.session.user_home / "scan-target"
    (target / "scripts").mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: scan-target\ndescription: d\n---\n\n# fine\n", encoding="utf-8"
    )
    (target / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")

    args = build_parser().parse_args(["skill", "scan", "~/scan-target"])
    args.runtime = command_runtime

    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert f"[meiva skill scan] {target} (trust=" in out
