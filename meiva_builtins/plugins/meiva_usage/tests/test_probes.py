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
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "file-stale-token",
                    "expiresAt": 1700000000000,
                }
            }
        ),
        encoding="utf-8",
    )

    keychain_store = {
        "unknown": json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "keychain-fresh-token",
                    "expiresAt": 1800000000000,
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


def test_probe_antigravity_token_refresh(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)

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

    ag_sec_name = "AG_SECRET"
    collector = CollectorConfig(
        id="ag-refresh",
        label="AG Refresh",
        provider="antigravity",
        machine="test-box",
        oauth_client_id_env="AG_ID",
        oauth_client_secret_env=ag_sec_name,
    )
    environ = {
        "AG_ID": "client-id-123",
        ag_sec_name: "client-sec-456",
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

    readings = probe_antigravity(
        collector,
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=fake_http_fetch,
    )
    assert readings["ag-refresh-gemini"].value == "7d 90% · 5h 50% left"
    assert readings["ag-refresh-gemini"].status == "ok"
    assert readings["ag-refresh-claude-gpt"].status == "error"
    assert any("oauth2.googleapis.com" in r.url for r in requests_made)


def test_probe_antigravity_email_filtering(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)

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


def test_probe_antigravity_dual_group_success_and_user_agent(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)

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
        id="ag-main",
        label="AG Main",
        provider="antigravity",
        machine="test-box",
    )

    captured_uas: list[str] = []
    requests_made: list[HttpJsonRequest] = []

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        requests_made.append(req)
        captured_uas.append(req.headers.get("User-Agent", ""))
        assert "retrieveUserQuotaSummary" in req.url
        assert req.headers.get("Authorization") == "Bearer valid-direct-token"
        return {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {"bucketId": "gemini-weekly", "remainingFraction": 0.8156919},
                            {"bucketId": "gemini-5h", "remainingFraction": 0.7631871},
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {"bucketId": "3p-weekly", "remainingFraction": 0.00902968},
                            {"bucketId": "3p-5h", "remainingFraction": 0.0563868},
                        ],
                    },
                ]
            }
        }

    readings = probe_antigravity(
        collector,
        user_home=tmp_path,
        environ={},
        now=now,
        platform="darwin",
        http_fetch=fake_http_fetch,
    )
    assert captured_uas[-1] == "antigravity/2.30.0 darwin/arm64"
    assert not any("oauth2.googleapis.com" in r.url for r in requests_made)
    assert "ag-main-gemini" in readings
    assert "ag-main-claude-gpt" in readings
    assert readings["ag-main-gemini"].status == "ok"
    assert readings["ag-main-gemini"].value == "7d 82% · 5h 76% left"
    assert readings["ag-main-claude-gpt"].status == "warn"
    assert readings["ag-main-claude-gpt"].value == "7d 1% · 5h 6% left"

    probe_antigravity(
        collector,
        user_home=tmp_path,
        environ={},
        now=now,
        platform="linux",
        http_fetch=fake_http_fetch,
    )
    assert captured_uas[-1] == "antigravity/2.30.0 linux/amd64"


def test_probe_antigravity_partial_gemini_only(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "valid-token",
                    "refresh_token": "valid-refresh",
                },
            }
        ),
        encoding="utf-8",
    )

    collector = CollectorConfig(
        id="ag-partial",
        label="AG Partial",
        provider="antigravity",
        machine="test-box",
    )

    def fake_gemini_only(_req: HttpJsonRequest) -> dict[str, Any]:
        return {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "gemini-5h", "remainingFraction": 0.9},
                    ],
                }
            ]
        }

    r1 = probe_antigravity(
        collector,
        user_home=tmp_path,
        environ={},
        now=now,
        platform="darwin",
        http_fetch=fake_gemini_only,
    )
    assert r1["ag-partial-gemini"].status == "ok"
    assert r1["ag-partial-gemini"].value == "90% left"
    assert r1["ag-partial-claude-gpt"].status == "error"
    assert r1["ag-partial-claude-gpt"].value == "—"
    assert "Claude and GPT models quota group missing" in r1["ag-partial-claude-gpt"].detail


def test_probe_antigravity_partial_claude_only(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "valid-token",
                    "refresh_token": "valid-refresh",
                },
            }
        ),
        encoding="utf-8",
    )

    collector = CollectorConfig(
        id="ag-partial",
        label="AG Partial",
        provider="antigravity",
        machine="test-box",
    )

    def fake_claude_only(_req: HttpJsonRequest) -> dict[str, Any]:
        return {
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "3p-5h", "remainingFraction": 0.8},
                    ],
                }
            ]
        }

    r2 = probe_antigravity(
        collector,
        user_home=tmp_path,
        environ={},
        now=now,
        platform="darwin",
        http_fetch=fake_claude_only,
    )
    assert r2["ag-partial-gemini"].status == "error"
    assert r2["ag-partial-gemini"].value == "—"
    assert "Gemini Models quota group missing" in r2["ag-partial-gemini"].detail
    assert r2["ag-partial-claude-gpt"].status == "ok"
    assert r2["ag-partial-claude-gpt"].value == "80% left"


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

    ag_sec_retry = "AG_SECRET"
    collector = CollectorConfig(
        id="ag-retry",
        label="AG Retry",
        provider="antigravity",
        machine="test-box",
        oauth_client_id_env="AG_ID",
        oauth_client_secret_env=ag_sec_retry,
    )
    environ = {"AG_ID": "id1", ag_sec_retry: "sec1"}

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

    readings = probe_antigravity(
        collector,
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=fake_http_fetch,
    )
    assert readings["ag-retry-gemini"].value == "90% left"
    assert readings["ag-retry-gemini"].status == "ok"
    assert readings["ag-retry-claude-gpt"].status == "error"
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

    found = find_codex_binary({}, user_home=tmp_path)
    assert found == str(fake_codex)


def test_codex_rpc_runner_interleaved_notifications_and_startup_noise(tmp_path: Path) -> None:
    mock_server = tmp_path / "mock_app_server.py"
    mock_server.write_text(
        """import json, sys

print("Codex app-server starting v1.0", flush=True)
print("Startup note: telemetry enabled", flush=True)

line1 = sys.stdin.readline()
req1 = json.loads(line1)
assert req1["id"] == 1

print(json.dumps({"jsonrpc": "2.0", "method": "notifications/startup", "params": {}}), flush=True)
print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "mock"}}}), flush=True)

line2 = sys.stdin.readline()
req2 = json.loads(line2)
assert req2["id"] == 2
assert req2["params"] is None

print(json.dumps({"jsonrpc": "2.0", "method": "notifications/indexing", "params": {}}), flush=True)
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
    assert reading["ds-card"].value == "CNY 50.00"
