from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from meiva_builtins.plugins.meiva_usage.parsers import (
    parse_antigravity_credentials_blob,
    parse_antigravity_windows,
    parse_claude_oauth_blob,
    parse_claude_windows,
    parse_codex_primary_window,
    sanitize_codex_rpc_error,
)
from meiva_builtins.plugins.meiva_usage.types import UsageProbeError, email_from_jwt


def test_parse_claude_oauth_blob_valid() -> None:
    expected = "test-access-token"
    data = {
        "claudeAiOauth": {
            "accessToken": expected,
            "expiresAt": 1790000000000,
        }
    }
    creds = parse_claude_oauth_blob(data)
    assert creds.access_token == expected
    assert creds.expires_at.year > 2025

    raw_json = json.dumps(data)
    creds_str = parse_claude_oauth_blob(raw_json)
    assert creds_str.access_token == expected


def test_parse_claude_oauth_blob_missing_token_raises() -> None:
    with pytest.raises(UsageProbeError, match=r"missing claudeAiOauth\.accessToken"):
        parse_claude_oauth_blob({"claudeAiOauth": {"expiresAt": 1790000000000}})


def test_parse_claude_oauth_blob_missing_expires_at_defaults_to_epoch() -> None:
    expected = "tok-without-expiry"
    creds = parse_claude_oauth_blob({"claudeAiOauth": {"accessToken": expected}})
    assert creds.access_token == expected
    assert creds.expires_at == datetime.fromtimestamp(0, tz=UTC)


def test_parse_claude_windows_modern() -> None:
    payload = {
        "limits": [
            {
                "kind": "session",
                "percent": 25.0,
                "resets_at": "2026-11-01T15:00:00Z",
            },
            {
                "kind": "weekly_all",
                "percent": 75.0,
                "resets_at": "2026-11-07T00:00:00Z",
            },
        ]
    }
    session, weekly = parse_claude_windows(payload)
    assert session is not None
    assert session.used_percent == 25.0
    assert session.window_minutes == 300
    assert session.resets_at == datetime(2026, 11, 1, 15, 0, tzinfo=UTC)

    assert weekly is not None
    assert weekly.used_percent == 75.0
    assert weekly.window_minutes == 10080
    assert weekly.resets_at == datetime(2026, 11, 7, 0, 0, tzinfo=UTC)


def test_parse_claude_windows_legacy() -> None:
    payload = {
        "five_hour": {
            "utilization": 15.0,
            "resets_at": "2026-11-01T12:00:00Z",
        },
        "seven_day": {
            "utilization": 50.0,
            "resets_at": "2026-11-05T00:00:00Z",
        },
    }
    session, weekly = parse_claude_windows(payload)
    assert session is not None
    assert session.used_percent == 15.0
    assert weekly is not None
    assert weekly.used_percent == 50.0


def test_email_from_jwt() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    payload = (
        base64.urlsafe_b64encode(b'{"email":"agent@example.com","sub":"123"}')
        .decode("ascii")
        .rstrip("=")
    )
    jwt_token = f"{header}.{payload}.sig"

    assert email_from_jwt(jwt_token) == "agent@example.com"
    assert email_from_jwt("invalid-jwt") is None
    assert email_from_jwt("") is None


def test_parse_codex_primary_window() -> None:
    p1 = {
        "rateLimits": {
            "primary": {
                "usedPercent": 42.0,
                "windowDurationMins": 1440,
                "resetsAt": 1790000000,
            }
        }
    }
    w1 = parse_codex_primary_window(p1)
    assert w1.used_percent == 42.0
    assert w1.window_minutes == 1440
    assert w1.resets_at is not None

    p2 = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "usedPercent": 10.0,
                    "windowDurationMins": 300,
                }
            }
        }
    }
    w2 = parse_codex_primary_window(p2)
    assert w2.used_percent == 10.0
    assert w2.window_minutes == 300


def test_sanitize_codex_rpc_error() -> None:
    assert (
        sanitize_codex_rpc_error({"message": "token_revoked by server"})
        == "Session expired (token_revoked); sign in again"
    )
    assert (
        sanitize_codex_rpc_error({"code": 401, "message": "unauthorized"})
        == "Unauthorized (401); sign in again"
    )
    assert (
        sanitize_codex_rpc_error({"code": 403, "message": "forbidden"})
        == "Forbidden (403); check permissions"
    )
    assert (
        sanitize_codex_rpc_error({"code": 429, "message": "rate limit"})
        == "Rate limit exceeded; retry later"
    )

    err_with_bearer = "Error with Bearer ya29.a0AfH6SMD_1234567890abcdef"
    sanitized = sanitize_codex_rpc_error(err_with_bearer)
    assert "ya29" not in sanitized
    assert "[REDACTED]" in sanitized


def test_parse_antigravity_credentials_blob() -> None:
    tok_access = "test-ag-access"
    tok_refresh = "test-ag-refresh"
    sample_key = "test-client-secret"
    blob = {
        "token": {
            "access_token": tok_access,
            "refresh_token": tok_refresh,
            "expiry": "2026-11-01T20:00:00Z",
            "client_id": "test-client-id",
            "client_secret": sample_key,
        },
        "id_token_email": "dev@example.com",
    }
    creds = parse_antigravity_credentials_blob(blob)
    assert creds.access_token == tok_access
    assert creds.refresh_token == tok_refresh
    assert creds.client_id == "test-client-id"
    assert creds.client_secret == sample_key
    assert creds.email == "dev@example.com"


def test_parse_antigravity_credentials_empty_access_token_with_refresh() -> None:
    expected_refresh = "valid-refresh-token"
    blob = {
        "token": {
            "access_token": "",
            "refresh_token": expected_refresh,
        }
    }
    creds = parse_antigravity_credentials_blob(blob)
    assert creds.access_token == ""
    assert creds.refresh_token == expected_refresh

    with pytest.raises(UsageProbeError, match="missing access_token and refresh_token"):
        parse_antigravity_credentials_blob({"token": {"access_token": "", "refresh_token": ""}})


def test_parse_antigravity_windows_wire_shape_wrapped_and_disabled() -> None:
    payload = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini",
                    "buckets": [
                        {
                            "bucketId": "gemini-2.5-pro-5h",
                            "remainingFraction": 0.75,
                            "resetTime": "2026-11-01T18:00:00Z",
                        },
                        {
                            "bucketId": "gemini-2.5-pro-weekly",
                            "disabled": True,
                            "resetTime": "2026-11-07T00:00:00Z",
                        },
                    ],
                }
            ]
        }
    }
    session, weekly = parse_antigravity_windows(payload)
    assert session is not None
    assert session.used_percent == 25.0
    assert session.window_minutes == 300

    assert weekly is not None
    assert weekly.used_percent == 100.0
    assert weekly.window_minutes == 10080


def test_parse_antigravity_windows_protobuf_omitted_zero() -> None:
    payload = {
        "groups": [
            {
                "name": "gemini-models",
                "buckets": [
                    {
                        "window": "5h",
                        "resetTime": "2026-11-01T18:00:00Z",
                    },
                    {
                        "window": "weekly",
                        "remainingFraction": 0.8,
                        "resetTime": "2026-11-07T00:00:00Z",
                    },
                    {
                        "window": "5h",
                        "disabled": True,
                        "remainingFraction": 1.0,
                    },
                ],
            }
        ]
    }
    session, weekly = parse_antigravity_windows(payload)
    assert session is not None
    assert session.used_percent == 100.0
    assert session.window_minutes == 300

    assert weekly is not None
    assert weekly.used_percent == 20.0
    assert weekly.window_minutes == 10080
