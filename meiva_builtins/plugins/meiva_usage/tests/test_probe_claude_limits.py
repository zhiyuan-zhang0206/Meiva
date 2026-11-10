# meiva:note
# Claude usage probe: token expiry, the 401/403 wording, and HTTP 429 as a distinct error.
# meiva:link meiva_builtins/plugins/meiva_usage/README.md

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.probes import (
    HttpJsonRequest,
    HttpStatusError,
    probe_claude,
    probe_deepseek,
)
from meiva_builtins.plugins.meiva_usage.types import UsageProbeError, UsageRateLimitedError

# meiva:note
# Before every `expiresAt` these tests write.
_CLAUDE_NOW = datetime(2023, 1, 1, tzinfo=UTC)


def _claude_collector(creds_file: Path) -> CollectorConfig:
    return CollectorConfig(
        id="claude-card",
        label="Claude",
        provider="claude",
        machine="test-box",
        credentials_path=str(creds_file),
    )


def _write_claude_creds(path: Path, *, expires_at_ms: int | None) -> None:
    oauth: dict[str, object] = {"accessToken": "tok"}
    if expires_at_ms is not None:
        oauth["expiresAt"] = expires_at_ms
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")


def _http_error(status: int) -> Any:
    def fetch(_req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(status, "nope")

    return fetch


def test_probe_claude_expired_token_does_not_call_the_api(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    _write_claude_creds(creds_file, expires_at_ms=1_700_000_000_000)
    calls: list[HttpJsonRequest] = []

    def fetch(req: HttpJsonRequest) -> dict[str, Any]:
        calls.append(req)
        return {}

    with pytest.raises(UsageProbeError) as caught:
        probe_claude(
            _claude_collector(creds_file),
            user_home=tmp_path,
            environ={},
            now=datetime(2026, 1, 1, tzinfo=UTC),
            platform="darwin",
            http_fetch=fetch,
        )
    assert str(caught.value) == "Claude access token expired; waiting for Claude Code to refresh it"
    assert "sign in again" not in str(caught.value)
    assert not isinstance(caught.value, UsageRateLimitedError)
    assert calls == []


def test_probe_claude_token_without_expiry_is_still_tried(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    _write_claude_creds(creds_file, expires_at_ms=None)

    def fetch(_req: HttpJsonRequest) -> dict[str, Any]:
        return {"limits": [{"kind": "session", "percent": 10.0}]}

    reading = probe_claude(
        _claude_collector(creds_file),
        user_home=tmp_path,
        environ={},
        now=datetime(2026, 1, 1, tzinfo=UTC),
        platform="darwin",
        http_fetch=fetch,
    )
    assert reading.value == "90% left"


@pytest.mark.parametrize("status", [401, 403])
def test_probe_claude_real_unauthorized_keeps_the_sign_in_message(
    tmp_path: Path, status: int
) -> None:
    creds_file = tmp_path / "creds.json"
    _write_claude_creds(creds_file, expires_at_ms=1_790_000_000_000)
    with pytest.raises(UsageProbeError, match=rf"unauthorized \({status}\); sign in again"):
        probe_claude(
            _claude_collector(creds_file),
            user_home=tmp_path,
            environ={},
            now=_CLAUDE_NOW,
            platform="darwin",
            http_fetch=_http_error(status),
        )


def test_probe_claude_429_raises_the_rate_limited_error(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    _write_claude_creds(creds_file, expires_at_ms=1_790_000_000_000)
    with pytest.raises(UsageRateLimitedError, match="HTTP 429"):
        probe_claude(
            _claude_collector(creds_file),
            user_home=tmp_path,
            environ={},
            now=_CLAUDE_NOW,
            platform="darwin",
            http_fetch=_http_error(429),
        )


def test_probe_claude_other_http_errors_are_plain_probe_errors(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    _write_claude_creds(creds_file, expires_at_ms=1_790_000_000_000)
    with pytest.raises(UsageProbeError, match="HTTP 500") as caught:
        probe_claude(
            _claude_collector(creds_file),
            user_home=tmp_path,
            environ={},
            now=_CLAUDE_NOW,
            platform="darwin",
            http_fetch=_http_error(500),
        )
    assert not isinstance(caught.value, UsageRateLimitedError)


def test_probe_deepseek_429_raises_the_rate_limited_error() -> None:
    collector = CollectorConfig(
        id="deepseek-card",
        label="DeepSeek",
        provider="deepseek",
        machine="test-box",
        key_env="MY_KEY",
    )
    with pytest.raises(UsageRateLimitedError):
        probe_deepseek(
            collector,
            user_home=Path("/nonexistent"),
            environ={"MY_KEY": "k"},
            now=datetime.now(UTC),
            platform="darwin",
            http_fetch=_http_error(429),
        )
