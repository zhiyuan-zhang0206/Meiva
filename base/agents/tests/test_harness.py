from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from base.agents.birth.harness import (
    DEFAULT_HARNESS,
    HarnessProbe,
    coerce_harness,
    probe_local_harnesses,
)
from base.api_contracts.agent_contract import ForkHarnessMismatch, Harness

_BIN_DIRS = (".local/bin", "bin")


def _external_cli(home: Path, relative_dir: str, name: str) -> Path:
    binary = home / relative_dir / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _iso(offset: timedelta) -> str:
    return (datetime.now(UTC) + offset).isoformat()


def _epoch_ms(offset: timedelta) -> int:
    return int((datetime.now(UTC) + offset).timestamp() * 1000)


def _claude_credentials(home: Path, *, access: timedelta, refresh: timedelta) -> None:
    path = home / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access",
                    "refreshToken": "refresh",
                    "expiresAt": _epoch_ms(access),
                    "refreshTokenExpiresAt": _epoch_ms(refresh),
                }
            }
        ),
        encoding="utf-8",
    )


def _antigravity_token(home: Path, *, relative: str, expiry: timedelta, refresh: bool) -> None:
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    token = {"access_token": "access", "expiry": _iso(expiry)}
    if refresh:
        token.update({"refresh_token": "renewable"})
    path.write_text(json.dumps({"token": token}), encoding="utf-8")


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
    _claude_credentials(tmp_path, access=timedelta(hours=8), refresh=timedelta(days=30))
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "available"
    assert row.binary == str(tmp_path / ".local" / "bin" / "claude")
    assert row.on_path is False


def test_a_recorded_account_without_a_token_is_unknown_not_available(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "account"}}), encoding="utf-8"
    )
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "unknown"
    assert "stores no token" in row.reason


def test_an_expired_refresh_token_is_unavailable(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    _claude_credentials(tmp_path, access=timedelta(hours=8), refresh=timedelta(days=-1))
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "unavailable"
    assert "expired at" in row.reason


def test_an_expired_access_token_is_a_pending_renewal_not_a_logout(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "claude")
    _claude_credentials(tmp_path, access=timedelta(hours=-1), refresh=timedelta(days=10))
    row = _probe(tmp_path)[Harness.CLAUDE_CODE]
    assert row.status == "available"
    assert "valid until" in row.reason


def test_a_codex_token_set_is_available_and_reports_its_age(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "codex")
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps(
            {
                "tokens": {"access_token": "access", "refresh_token": "refresh"},
                "last_refresh": "2026-10-18T12:19:55Z",
            }
        ),
        encoding="utf-8",
    )
    row = _probe(tmp_path)[Harness.CODEX]
    assert row.status == "available"
    assert "last refreshed 2026-10-18T12:19:55Z" in row.reason


def test_an_antigravity_session_reports_its_access_expiry_as_age(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")
    _antigravity_token(
        tmp_path,
        relative=".gemini/antigravity-cli/antigravity-oauth-token",
        expiry=timedelta(hours=-1),
        refresh=True,
    )
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "available"
    assert "renewed on use" in row.reason


def test_an_antigravity_session_without_a_refresh_token_expires(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")
    _antigravity_token(
        tmp_path,
        relative=".gemini/antigravity-cli/antigravity-oauth-token",
        expiry=timedelta(hours=-1),
        refresh=False,
    )
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "unavailable"
    assert "expired at" in row.reason


def test_the_legacy_antigravity_token_file_still_answers(tmp_path: Path) -> None:
    _external_cli(tmp_path, ".local/bin", "agy")
    _antigravity_token(
        tmp_path,
        relative=".gemini/jetski-standalone-oauth-token",
        expiry=timedelta(hours=1),
        refresh=True,
    )
    row = _probe(tmp_path)[Harness.ANTIGRAVITY]
    assert row.status == "available"
    assert "jetski-standalone-oauth-token" in row.reason


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
