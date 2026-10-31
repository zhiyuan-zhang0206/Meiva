"""End-to-end unit tests for usage probes using pure dependency injection."""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.probes import (
    HttpJsonRequest,
    HttpStatusError,
    default_codex_rpc_runner,
    find_codex_binary,
    probe_antigravity,
    probe_claude,
    probe_codex,
    probe_collector,
    probe_deepseek,
)
from meiva_builtins.plugins.meiva_usage.types import UsageProbeError

# ---------------------------------------------------------------------------
# DeepSeek Probe Tests
# ---------------------------------------------------------------------------


def test_probe_deepseek_success() -> None:
    collector = CollectorConfig(
        id="deepseek-card",
        label="DeepSeek",
        provider="deepseek",
        machine="test-box",
        key_env="MY_DEEPSEEK_KEY",
    )
    environ = {"MY_DEEPSEEK_KEY": "sk-test-secret-key"}

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        assert req.url == "https://api.deepseek.com/user/balance"
        assert req.headers.get("Authorization") == "Bearer sk-test-secret-key"
        return {
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "88.50"}],
        }

    reading = probe_deepseek(
        collector,
        user_home=Path("/nonexistent"),
        environ=environ,
        now=datetime.now(UTC),
        platform="darwin",
        http_fetch=fake_http_fetch,
    )
    assert reading.value == "CNY 88.50"
    assert reading.status == "ok"


def test_probe_deepseek_missing_env_raises() -> None:
    collector = CollectorConfig(
        id="deepseek-card",
        label="DeepSeek",
        provider="deepseek",
        machine="test-box",
        key_env="UNSET_KEY_VAR",
    )
    with pytest.raises(UsageProbeError, match="Environment variable UNSET_KEY_VAR is not set"):
        probe_deepseek(
            collector,
            user_home=Path("/nonexistent"),
            environ={},
            now=datetime.now(UTC),
            platform="darwin",
            http_fetch=lambda _req: {},
        )


def test_probe_deepseek_401_raises() -> None:
    collector = CollectorConfig(
        id="deepseek-card",
        label="DeepSeek",
        provider="deepseek",
        machine="test-box",
        key_env="MY_KEY",
    )

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(401, "Unauthorized")

    with pytest.raises(UsageProbeError, match="is invalid \\(401\\)"):
        probe_deepseek(
            collector,
            user_home=Path("/nonexistent"),
            environ={"MY_KEY": "invalid-token"},
            now=datetime.now(UTC),
            platform="darwin",
            http_fetch=fake_http_fetch,
        )


# ---------------------------------------------------------------------------
# Claude Probe Tests
# ---------------------------------------------------------------------------


def test_probe_claude_explicit_path(tmp_path: Path) -> None:
    creds_file = tmp_path / "custom_claude.json"
    creds_file.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "custom-token",
                    "expiresAt": 1790000000000,
                }
            }
        ),
        encoding="utf-8",
    )

    collector = CollectorConfig(
        id="claude-card",
        label="Claude",
        provider="claude",
        machine="test-box",
        credentials_path=str(creds_file),
    )

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        assert req.headers.get("Authorization") == "Bearer custom-token"
        return {
            "limits": [
                {"kind": "session", "percent": 30.0},
                {"kind": "weekly_all", "percent": 50.0},
            ]
        }

    reading = probe_claude(
        collector,
        user_home=tmp_path,
        environ={},
        now=datetime.now(UTC),
        platform="darwin",
        http_fetch=fake_http_fetch,
    )
    assert reading.value == "50% left"
    assert reading.status == "ok"


def test_probe_claude_freshest_expiry_selection(tmp_path: Path) -> None:
    # Stale file credential
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "file-stale-token",
                    "expiresAt": 1700000000000,  # Older
                }
            }
        ),
        encoding="utf-8",
    )

    # Fresh keychain credential
    keychain_store = {
        "unknown": json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "keychain-fresh-token",
                    "expiresAt": 1800000000000,  # Fresher
                }
            }
        )
    }

    collector = CollectorConfig(
        id="claude-auto",
        label="Claude Auto",
        provider="claude",
        machine="test-box",
    )

    seen_tokens: list[str] = []

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        token = str(req.headers.get("Authorization", "")).removeprefix("Bearer ")
        seen_tokens.append(token)
        return {
            "limits": [{"kind": "session", "percent": 10.0}],
        }

    reading = probe_claude(
        collector,
        user_home=tmp_path,
        environ={},
        now=datetime.now(UTC),
        platform="darwin",
        http_fetch=fake_http_fetch,
        keychain_reader=keychain_store.get,
    )
    assert reading.value == "90% left"
    assert seen_tokens == ["keychain-fresh-token"]


def test_probe_claude_missing_credentials_raises(tmp_path: Path) -> None:
    collector = CollectorConfig(
        id="claude-none",
        label="Claude None",
        provider="claude",
        machine="test-box",
    )
    with pytest.raises(UsageProbeError, match="No Claude Code credentials found"):
        probe_claude(
            collector,
            user_home=tmp_path,
            environ={},
            now=datetime.now(UTC),
            platform="darwin",
            http_fetch=lambda _req: {},
            keychain_reader=lambda _acct: None,
        )


# ---------------------------------------------------------------------------
# Antigravity Probe Tests
# ---------------------------------------------------------------------------


def test_probe_antigravity_token_refresh(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    # Expired token (expiry 10 minutes in the past)
    expired_dt = (now - timedelta(minutes=10)).isoformat()
    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "expired-access-token",
                    "refresh_token": "valid-refresh-token",
                    "expiry": expired_dt,
                },
                "id_token_email": "dev@example.com",
            }
        ),
        encoding="utf-8",
    )

    collector = CollectorConfig(
        id="ag-refresh",
        label="AG Refresh",
        provider="antigravity",
        machine="test-box",
        oauth_client_id_env="AG_ID",
        oauth_client_secret_env="AG_SECRET",  # noqa: S106
    )
    environ = {
        "AG_ID": "client-id-123",
        "AG_SECRET": "client-sec-456",
    }

    requests_made: list[HttpJsonRequest] = []

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        requests_made.append(req)
        if "oauth2.googleapis.com" in req.url:
            return {"access_token": "refreshed-access-token", "expires_in": 3600}
        if "retrieveUserQuotaSummary" in req.url:
            assert req.headers.get("Authorization") == "Bearer refreshed-access-token"
            return {
                "groups": [
                    {
                        "name": "gemini-quota",
                        "buckets": [
                            {"window": "5h", "remainingFraction": 0.5},
                            {"window": "weekly", "remainingFraction": 0.9},
                        ],
                    }
                ]
            }
        return {}

    reading = probe_antigravity(
        collector,
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=fake_http_fetch,
    )
    # Refreshed token was used, 5h remaining 50%, weekly remaining 90% -> 50% left
    assert reading.value == "50% left"
    assert reading.status == "ok"
    assert any("oauth2.googleapis.com" in r.url for r in requests_made)


def test_probe_antigravity_email_filtering(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    # File with different email
    future_dt = (now + timedelta(hours=2)).isoformat()
    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "token": {"access_token": "other-token", "expiry": future_dt},
                "id_token_email": "other@example.com",
            }
        ),
        encoding="utf-8",
    )

    collector = CollectorConfig(
        id="ag-email",
        label="AG Email",
        provider="antigravity",
        machine="test-box",
        account_email="wanted@example.com",
    )

    with pytest.raises(UsageProbeError, match="No Antigravity credentials found for account"):
        probe_antigravity(
            collector,
            user_home=tmp_path,
            environ={},
            now=now,
            platform="darwin",
            http_fetch=lambda _req: {},
        )


def test_probe_antigravity_no_expiry_does_not_refresh_upfront(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    # Token with non-empty access_token and expiry=None
    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "valid-direct-token",
                    "refresh_token": "valid-refresh-token",
                },
                "id_token_email": "dev@example.com",
            }
        ),
        encoding="utf-8",
    )

    collector = CollectorConfig(
        id="ag-direct",
        label="AG Direct",
        provider="antigravity",
        machine="test-box",
    )

    requests_made: list[HttpJsonRequest] = []

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        requests_made.append(req)
        assert "retrieveUserQuotaSummary" in req.url
        assert req.headers.get("Authorization") == "Bearer valid-direct-token"
        return {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini",
                        "buckets": [
                            {"bucketId": "gemini-2.5-pro-5h", "remainingFraction": 0.8},
                            {"bucketId": "gemini-2.5-pro-weekly", "remainingFraction": 0.6},
                        ],
                    }
                ]
            }
        }

    reading = probe_antigravity(
        collector,
        user_home=tmp_path,
        environ={},
        now=now,
        platform="darwin",
        http_fetch=fake_http_fetch,
    )
    assert reading.value == "60% left"
    assert not any("oauth2.googleapis.com" in r.url for r in requests_made)


def test_probe_antigravity_401_triggers_refresh_retry(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "stale-token",
                    "refresh_token": "valid-refresh",
                },
            }
        ),
        encoding="utf-8",
    )

    collector = CollectorConfig(
        id="ag-retry",
        label="AG Retry",
        provider="antigravity",
        machine="test-box",
        oauth_client_id_env="AG_ID",
        oauth_client_secret_env="AG_SECRET",  # noqa: S106
    )
    environ = {"AG_ID": "id1", "AG_SECRET": "sec1"}

    attempts = {"quota_calls": 0, "refreshed": False}

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        if "oauth2.googleapis.com" in req.url:
            attempts["refreshed"] = True
            return {"access_token": "fresh-token", "expires_in": 3600}
        if "retrieveUserQuotaSummary" in req.url:
            attempts["quota_calls"] += 1
            if attempts["quota_calls"] == 1:
                raise HttpStatusError(401, "Unauthorized")
            assert req.headers.get("Authorization") == "Bearer fresh-token"
            return {
                "groups": [
                    {
                        "name": "gemini",
                        "buckets": [
                            {"bucketId": "gemini-5h", "remainingFraction": 0.9},
                        ],
                    }
                ]
            }
        return {}

    reading = probe_antigravity(
        collector,
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=fake_http_fetch,
    )
    assert reading.value == "90% left"
    assert attempts["quota_calls"] == 2
    assert attempts["refreshed"] is True


def test_probe_antigravity_non_auth_http_error(tmp_path: Path) -> None:
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps({"token": {"access_token": "tok", "refresh_token": "ref"}}),
        encoding="utf-8",
    )

    collector = CollectorConfig(
        id="ag-error",
        label="AG Error",
        provider="antigravity",
        machine="test-box",
    )

    def fake_http_fetch(_req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(500, "Internal Server Error")

    with pytest.raises(UsageProbeError, match="Antigravity quota request failed with HTTP 500"):
        probe_antigravity(
            collector,
            user_home=tmp_path,
            environ={},
            now=datetime.now(UTC),
            platform="darwin",
            http_fetch=fake_http_fetch,
        )


# ---------------------------------------------------------------------------
# Codex Probe Tests
# ---------------------------------------------------------------------------


def test_probe_codex_multi_home_failover(tmp_path: Path) -> None:
    home1 = tmp_path / ".codex-home1"
    home2 = tmp_path / ".codex-home2"
    home1.mkdir()
    home2.mkdir()

    collector = CollectorConfig(
        id="codex-failover",
        label="Codex Failover",
        provider="codex",
        machine="test-box",
        account_email="user@example.com",
    )

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(b'{"email":"user@example.com"}').decode("ascii").rstrip("=")
    jwt_token = f"{header}.{payload}.sig"

    # Both homes match account_email
    (home1 / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {"id_token": jwt_token, "last_refresh": "2026-11-01T12:00:00Z"},
            }
        ),
        encoding="utf-8",
    )
    (home2 / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {"id_token": jwt_token, "last_refresh": "2026-11-01T11:00:00Z"},
            }
        ),
        encoding="utf-8",
    )

    tried_homes: list[Path] = []

    def fake_rpc_runner(home: Path, env: Mapping[str, str], timeout: float) -> dict[str, Any]:
        tried_homes.append(home)
        if home == home1:
            raise UsageProbeError("Session expired (token_revoked); sign in again")
        # home2 succeeds
        return {
            "rateLimits": {
                "primary": {
                    "usedPercent": 25.0,
                    "windowDurationMins": 10080,
                }
            }
        }

    reading = probe_codex(
        collector,
        user_home=tmp_path,
        environ={},
        now=datetime.now(UTC),
        platform="darwin",
        codex_rpc_runner=fake_rpc_runner,
    )
    assert reading.value == "75% left"
    assert tried_homes == [home1, home2]


def test_find_codex_binary_fallback_to_user_home(tmp_path: Path) -> None:
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    fake_codex = local_bin / "codex"
    fake_codex.write_text("#!/bin/sh\necho ok")
    fake_codex.chmod(0o755)

    # Empty PATH, but user_home provided
    found = find_codex_binary({}, user_home=tmp_path)
    assert found == str(fake_codex)


def test_codex_rpc_runner_interleaved_notifications_and_startup_noise(tmp_path: Path) -> None:
    mock_server = tmp_path / "mock_app_server.py"
    mock_server.write_text(
        """import json, sys

print("Codex app-server starting v1.0", flush=True)
print("Startup note: telemetry enabled", flush=True)

# 1. Read initialize request
line1 = sys.stdin.readline()
req1 = json.loads(line1)
assert req1["id"] == 1

# Notification before reply
print(json.dumps({"jsonrpc": "2.0", "method": "notifications/startup", "params": {}}), flush=True)

# Initialize reply
print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "mock"}}}), flush=True)

# 2. Read rateLimits request
line2 = sys.stdin.readline()
req2 = json.loads(line2)
assert req2["id"] == 2
assert req2["params"] is None

# Notification before rateLimits reply
print(json.dumps({"jsonrpc": "2.0", "method": "notifications/indexing", "params": {}}), flush=True)

# RateLimits reply
print(json.dumps({
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "rateLimits": {
            "primary": {
                "usedPercent": 35.0,
                "windowDurationMins": 300,
            }
        }
    }
}), flush=True)
""",
        encoding="utf-8",
    )

    mock_codex = tmp_path / "mock_codex"
    mock_codex.write_text(
        f"#!{sys.executable}\nimport sys; exec(open({str(mock_server)!r}).read())\n"
    )
    mock_codex.chmod(0o755)

    def mock_finder(_env: Mapping[str, str], *, user_home: Path | None = None) -> str:
        del user_home
        return str(mock_codex)

    res = default_codex_rpc_runner(
        tmp_path / ".codex",
        environ={},
        timeout_s=5.0,
        binary_finder=mock_finder,
    )
    assert "rateLimits" in res
    rate_limits: Any = res["rateLimits"]
    assert rate_limits["primary"]["usedPercent"] == 35.0


# ---------------------------------------------------------------------------
# Dispatcher Test
# ---------------------------------------------------------------------------


def test_probe_collector_dispatcher() -> None:
    c = CollectorConfig(
        id="ds-card",
        label="DeepSeek",
        provider="deepseek",
        machine="test-box",
        key_env="DS_KEY",
    )

    def fake_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        return {
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "50.00"}],
        }

    reading = probe_collector(
        c,
        user_home=Path("/nonexistent"),
        environ={"DS_KEY": "sk-test"},
        now=datetime.now(UTC),
        platform="darwin",
        http_fetch=fake_fetch,
    )
    assert reading.value == "CNY 50.00"
