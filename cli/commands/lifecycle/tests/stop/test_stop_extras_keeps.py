# meiva:note
# `--stop-extras` is refused, before any unit stops, when a keep choice conflicts with it.
# Retiring the permissions helper needs the service root stopped, and the root exits only when no
# unit stays behind for it to host.

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import stop as entry


@pytest.mark.parametrize(
    ("keep_shell_sessions", "keep_browser", "preserve_sessions", "named"),
    [
        (True, False, frozenset[str](), "--keep-shell-sessions"),
        (False, True, frozenset[str](), "--keep-browser"),
        (False, False, frozenset({"meiva-ops"}), "--keep-service"),
    ],
)
def test_each_keep_choice_conflicts_with_retiring_extras(
    keep_shell_sessions: bool,
    keep_browser: bool,
    preserve_sessions: frozenset[str],
    named: str,
) -> None:
    with pytest.raises(ValueError) as refusal:
        command.require_extras_retirable(
            teardown_extras=True,
            keep_shell_sessions=keep_shell_sessions,
            keep_browser=keep_browser,
            preserve_sessions=preserve_sessions,
        )
    message = str(refusal.value)
    assert named in message
    # meiva:note
    # Both ways out are named: the operator never has to guess.
    assert "Drop them, or use --keep-extras" in message


@pytest.mark.parametrize(
    ("teardown_extras", "keep_shell_sessions", "keep_browser", "preserve_sessions"),
    [
        (True, False, False, frozenset[str]()),
        (False, True, True, frozenset({"meiva-ops"})),
    ],
)
def test_a_compatible_stop_is_admitted(
    teardown_extras: bool,
    keep_shell_sessions: bool,
    keep_browser: bool,
    preserve_sessions: frozenset[str],
) -> None:
    command.require_extras_retirable(
        teardown_extras=teardown_extras,
        keep_shell_sessions=keep_shell_sessions,
        keep_browser=keep_browser,
        preserve_sessions=preserve_sessions,
    )


@pytest.mark.parametrize(
    ("force", "require_confirmation"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_the_stop_kernel_refuses_before_it_touches_a_single_dependency(
    force: bool, require_confirmation: bool
) -> None:
    """Both stop paths meet in `do_stop`; the guard must run before either one starts work.

    Nothing here is patched: the dependencies are fakes passed in, so any call they
    receive is work that ran before the refusal (a studio database read, a telemetry
    pipeline, a machine identity lookup). All of them must stay untouched.
    """
    database_factory: Any = MagicMock()
    producer: Any = MagicMock()
    machine: Any = MagicMock()
    with pytest.raises(ValueError, match="retires the permissions helper"):
        entry.do_stop(
            Path("/nonexistent-home"),
            require_confirmation=require_confirmation,
            keep_infra=None,
            preserve_sessions=frozenset[str](),
            keep_browser=True,
            keep_shell_sessions=False,
            announce=False,
            teardown_extras=True,
            force=force,
            timeout=30.0,
            database_factory=database_factory,
            producer=producer,
            machine=machine,
            helper=None,
        )
    # `require_confirmation=True` proves the refusal precedes the interactive prompt: a prompt
    # first would consume stdin and never reach this assertion.
    assert database_factory.mock_calls == []
    assert producer.mock_calls == []
    assert machine.mock_calls == []


@pytest.mark.parametrize("retained", [frozenset({"browser"}), frozenset({"meiva-pty-sessions"})])
def test_retiring_extras_itself_refuses_while_a_unit_is_retained(retained: frozenset[str]) -> None:
    """The action, not only the command, refuses: `stop_permissions_helper` stays unbypassable."""
    with pytest.raises(ValueError) as refusal:
        command.stop_extras(30.0, retained=retained)
    assert "retains" in str(refusal.value)


def test_every_call_site_passes_the_retained_keyword() -> None:
    """A call site that forgot `retained` stopped every unit and then raised TypeError.

    It ran after the tree was down, outside the failure reporting that turns a stop into a
    readable message (review 2026-10-22), so the signature is pinned here for every caller.
    """
    calls = 0
    for path in sorted((Path(__file__).resolve().parents[2]).glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "stop_extras":
                continue
            calls += 1
            assert any(keyword.arg == "retained" for keyword in node.keywords), path
    assert calls >= 3
