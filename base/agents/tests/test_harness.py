from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from base.agents.birth.harness import (
    DEFAULT_HARNESS,
    LAUNCHABLE_HARNESSES,
    HarnessProbe,
    coerce_harness,
    probe_local_harnesses,
    resolve_birth_harness,
)
from base.api_contracts.agent_contract import (
    ErrorReason,
    ForkHarnessMismatch,
    Harness,
    HarnessNotLaunchable,
    MeivaAgentError,
)

_BIN_DIRS = (".local/bin", "bin")


def _external_cli(home: Path, relative_dir: str, name: str) -> Path:
    binary = home / relative_dir / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _probe(home: Path) -> dict[Harness, HarnessProbe]:
    rows = probe_local_harnesses(home, str(home / "empty-bin"), _BIN_DIRS)
    return {row.harness: row for row in rows}


def test_every_harness_gets_one_readable_row(tmp_path: Path) -> None:
    rows = probe_local_harnesses(tmp_path, str(tmp_path / "empty-bin"), _BIN_DIRS)
    assert [row.harness for row in rows] == list(Harness)
    assert all(row.reason for row in rows)


def test_the_builtin_runtime_needs_no_external_command(tmp_path: Path) -> None:
    row = _probe(tmp_path)[Harness.MEIVA]
    assert row.status == "available"
    assert row.binary is None


def test_a_missing_command_is_unavailable_and_names_what_was_searched(tmp_path: Path) -> None:
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "unavailable"
    assert "agy" in row.reason


def test_an_installed_signed_in_harness_is_available(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "account"}}), encoding="utf-8"
    )
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "available"
    assert row.binary == str(tmp_path / ".local" / "bin" / "claude")
    assert row.on_path is False


def test_a_path_install_is_reported_as_on_path(tmp_path: Path) -> None:
    binary = _external_cli(tmp_path, "bin", "codex")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "token"}}), encoding="utf-8"
    )
    rows = probe_local_harnesses(tmp_path, str(binary.parent), _BIN_DIRS)
    row = {candidate.harness: candidate for candidate in rows}[Harness.CODEX]
    assert row.status == "available"
    assert row.on_path is True


def test_an_installed_but_unsigned_harness_is_unavailable(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "unavailable"
    assert "not signed in" in row.reason


def test_an_unreadable_sign_in_state_is_unknown_not_a_guess(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "unknown"
    assert row.binary == str(tmp_path / ".local" / "bin" / "agy")
    assert "unknown" in row.reason


def test_coerce_harness_accepts_the_enum_and_refuses_unknown_names() -> None:
    assert coerce_harness(None) is DEFAULT_HARNESS
    assert coerce_harness("codex") is Harness.CODEX
    assert coerce_harness(Harness.ANTIGRAVITY) is Harness.ANTIGRAVITY
    with pytest.raises(ValueError, match="unknown harness") as refusal:
        coerce_harness("claude-code")
    assert "claude_code" in str(refusal.value)


def test_fork_harness_mismatch_is_a_fork_error() -> None:
    from base.api_contracts.agent_contract import ForkError, MeivaAgentError

    assert issubclass(ForkHarnessMismatch, ForkError)
    assert issubclass(ForkHarnessMismatch, MeivaAgentError)


class _UnreadCursor:
    def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("a plain birth reads no fork source")


def _no_cursor() -> psycopg.Cursor[Any]:
    return cast("psycopg.Cursor[Any]", _UnreadCursor())


# meiva:note
# Every declared harness has a launcher today, so the refusal is driven by handing
# the rule a narrower set: membership is what decides, and this pins the moment a
# launcher is retired or a harness is declared before one.
# meiva:link base/agents/docs/harness.meiva.okf.md
def test_a_harness_outside_the_launchable_set_is_refused() -> None:
    with pytest.raises(HarnessNotLaunchable) as refusal:
        resolve_birth_harness(
            _no_cursor(), Harness.CODEX, None, launchable=frozenset({Harness.MEIVA})
        )
    detail = str(refusal.value)
    assert "codex" in detail
    assert "meiva" in detail
    assert "claude_code" not in detail


def test_the_shipped_set_is_what_the_birth_gate_reads() -> None:
    for launchable in LAUNCHABLE_HARNESSES:
        assert resolve_birth_harness(_no_cursor(), launchable, None) == launchable.value


def test_every_launchable_harness_is_accepted_at_birth() -> None:
    for launchable in LAUNCHABLE_HARNESSES:
        assert resolve_birth_harness(_no_cursor(), launchable, None) == launchable.value


def test_harness_not_launchable_is_a_wire_error() -> None:
    assert issubclass(HarnessNotLaunchable, MeivaAgentError)
    assert HarnessNotLaunchable.reason is ErrorReason.HARNESS_NOT_LAUNCHABLE
    assert HarnessNotLaunchable.http_status == 400
