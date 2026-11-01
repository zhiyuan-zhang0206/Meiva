from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

from meiva_builtins.plugins.meiva_usage.credentials import (
    parse_antigravity_credentials_blob,
    parse_claude_oauth_blob,
)
from meiva_builtins.plugins.meiva_usage.parsers import (
    parse_anthropic_cost_report_page,
    parse_antigravity_quota_groups,
    parse_claude_windows,
    parse_codex_primary_window,
    parse_openai_costs_page,
    sanitize_codex_rpc_error,
)
from meiva_builtins.plugins.meiva_usage.types import (
    UsageProbeError,
    WindowPair,
    email_from_jwt,
)


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


def _assert_real_gemini_groups(gemini: WindowPair | None) -> None:
    assert gemini is not None
    assert gemini.weekly is not None
    assert gemini.session is not None
    assert pytest.approx(gemini.weekly.used_percent, rel=1e-4) == 18.4308
    assert pytest.approx(gemini.session.used_percent, rel=1e-4) == 23.6813
    assert gemini.weekly.window_minutes == 10080
    assert gemini.session.window_minutes == 300


def _assert_real_claude_gpt_groups(claude_gpt: WindowPair | None) -> None:
    assert claude_gpt is not None
    assert claude_gpt.weekly is not None
    assert claude_gpt.session is not None
    assert pytest.approx(claude_gpt.weekly.used_percent, rel=1e-4) == 99.097
    assert pytest.approx(claude_gpt.session.used_percent, rel=1e-4) == 94.3613
    assert claude_gpt.weekly.window_minutes == 10080
    assert claude_gpt.session.window_minutes == 300


def test_parse_antigravity_quota_groups_real_wire_payload() -> None:
    payload = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "remainingFraction": 0.8156919,
                        "resetTime": "2026-11-07T00:00:00Z",
                    },
                    {
                        "bucketId": "gemini-5h",
                        "remainingFraction": 0.7631871,
                        "resetTime": "2026-11-01T18:00:00Z",
                    },
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "remainingFraction": 0.00902968,
                        "resetTime": "2026-11-08T00:00:00Z",
                    },
                    {
                        "bucketId": "3p-5h",
                        "remainingFraction": 0.0563868,
                        "resetTime": "2026-11-01T17:00:00Z",
                    },
                ],
            },
        ]
    }
    groups = parse_antigravity_quota_groups(payload)
    _assert_real_gemini_groups(groups.gemini)
    _assert_real_claude_gpt_groups(groups.claude_gpt)


def test_parse_antigravity_quota_groups_wrapped_response_and_disabled() -> None:
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
    groups = parse_antigravity_quota_groups(payload)
    assert groups.gemini is not None
    assert groups.gemini.session is not None
    assert groups.gemini.session.used_percent == 25.0
    assert groups.gemini.weekly is not None
    assert groups.gemini.weekly.used_percent == 100.0
    assert groups.claude_gpt is None


def test_parse_antigravity_quota_groups_protobuf_omitted_zero() -> None:
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
    groups = parse_antigravity_quota_groups(payload)
    assert groups.gemini is not None
    assert groups.gemini.session is not None
    assert groups.gemini.session.used_percent == 100.0
    assert groups.gemini.weekly is not None
    assert groups.gemini.weekly.used_percent == 20.0


def test_parse_antigravity_quota_groups_flat_buckets_fallback() -> None:
    payload = {
        "buckets": [
            {
                "bucketId": "gemini-5h",
                "remainingFraction": 0.9,
            },
            {
                "bucketId": "3p-weekly",
                "remainingFraction": 0.4,
            },
        ]
    }
    groups = parse_antigravity_quota_groups(payload)
    assert groups.gemini is not None
    assert groups.gemini.session is not None
    assert groups.gemini.session.used_percent == 10.0
    assert groups.gemini.weekly is None

    assert groups.claude_gpt is not None
    assert groups.claude_gpt.weekly is not None
    assert groups.claude_gpt.weekly.used_percent == 60.0
    assert groups.claude_gpt.session is None


def test_parse_antigravity_quota_groups_empty_raises() -> None:
    with pytest.raises(UsageProbeError, match="missing rate-limit windows"):
        parse_antigravity_quota_groups({})

    with pytest.raises(UsageProbeError, match="missing rate-limit windows"):
        parse_antigravity_quota_groups({"groups": []})

    empty_buckets: list[object] = []
    with pytest.raises(UsageProbeError, match="missing rate-limit windows"):
        parse_antigravity_quota_groups({"groups": [{"name": "unknown", "buckets": empty_buckets}]})


def test_parse_openai_costs_page_basic() -> None:
    payload = {
        "data": [
            {
                "start_time": 1790000000,
                "results": [
                    {"amount": {"value": 1.75, "currency": "usd"}},
                    {"amount": {"value": 2, "currency": "usd"}},
                    {"amount": {"value": "0.50", "currency": "usd"}},
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    page = parse_openai_costs_page(payload)
    dt = datetime.fromtimestamp(1790000000, tz=UTC).date()
    assert page.daily_usd == {dt: Decimal("4.25")}
    assert page.next_page is None


def test_parse_openai_costs_page_multi_bucket() -> None:
    t1 = 1790000000
    t2 = 1790086400
    d1 = datetime.fromtimestamp(t1, tz=UTC).date()
    d2 = datetime.fromtimestamp(t2, tz=UTC).date()

    payload = {
        "data": [
            {
                "start_time": t1,
                "results": [
                    {"amount": {"value": 10.0, "currency": "usd"}},
                ],
            },
            {
                "start_time": t2,
                "results": [
                    {"amount": {"value": 5.25, "currency": "usd"}},
                    {"amount": {"value": 4.75, "currency": "usd"}},
                ],
            },
        ],
        "has_more": True,
        "next_page": "cursor_page_2",
    }
    page = parse_openai_costs_page(payload)
    assert page.daily_usd[d1] == Decimal("10.0")
    assert page.daily_usd[d2] == Decimal("10.00")
    assert page.next_page == "cursor_page_2"


def test_parse_openai_costs_page_pagination_errors() -> None:
    with pytest.raises(UsageProbeError, match="indicated has_more but next_page is missing"):
        parse_openai_costs_page({"data": [], "has_more": True, "next_page": None})

    with pytest.raises(UsageProbeError, match="indicated has_more but next_page is missing"):
        parse_openai_costs_page({"data": [], "has_more": True, "next_page": "   "})


def test_parse_openai_costs_page_currency_validation() -> None:
    bad_payload = {
        "data": [
            {
                "start_time": 1790000000,
                "results": [{"amount": {"value": 5.0, "currency": "eur"}}],
            }
        ]
    }
    with pytest.raises(UsageProbeError, match="Unsupported currency in OpenAI costs: eur"):
        parse_openai_costs_page(bad_payload)


def test_parse_openai_costs_page_invalid_payload() -> None:
    with pytest.raises(UsageProbeError, match="missing data list"):
        parse_openai_costs_page({})

    with pytest.raises(UsageProbeError, match="Invalid bucket"):
        parse_openai_costs_page({"data": ["not-a-dict"]})

    with pytest.raises(UsageProbeError, match="missing valid start_time"):
        parse_openai_costs_page({"data": [{"start_time": "invalid"}]})

    with pytest.raises(UsageProbeError, match="missing results list"):
        parse_openai_costs_page({"data": [{"start_time": 1790000000}]})

    with pytest.raises(UsageProbeError, match="missing amount object"):
        empty_res_payload: dict[str, object] = {
            "data": [{"start_time": 1790000000, "results": [cast(dict[str, object], {})]}]
        }
        parse_openai_costs_page(empty_res_payload)


def test_parse_anthropic_cost_report_page_basic() -> None:
    payload = {
        "data": [
            {
                "starting_at": "2026-10-01T00:00:00Z",
                "results": [
                    {"amount": "123.45", "currency": "USD"},
                    {"amount": "200", "currency": "USD"},
                ],
            }
        ],
        "has_more": False,
    }
    page = parse_anthropic_cost_report_page(payload)
    assert page.daily_usd == {date(2026, 10, 1): Decimal("3.2345")}
    assert page.next_page is None


def test_parse_anthropic_cost_report_page_multi_bucket_and_pagination() -> None:
    payload = {
        "data": [
            {
                "starting_at": "2026-10-01T00:00:00Z",
                "results": [{"amount": "1000", "currency": "USD"}],
            },
            {
                "starting_at": "2026-10-02T00:00:00Z",
                "results": [{"amount": "500", "currency": "USD"}],
            },
        ],
        "has_more": True,
        "next_page": "anthropic_cursor_123",
    }
    page = parse_anthropic_cost_report_page(payload)
    assert page.daily_usd[date(2026, 10, 1)] == Decimal("10.00")
    assert page.daily_usd[date(2026, 10, 2)] == Decimal("5.00")
    assert page.next_page == "anthropic_cursor_123"


def test_parse_anthropic_cost_report_page_pagination_errors() -> None:
    with pytest.raises(UsageProbeError, match="indicated has_more but next_page is missing"):
        parse_anthropic_cost_report_page({"data": [], "has_more": True, "next_page": None})


def test_parse_anthropic_cost_report_page_currency_validation() -> None:
    bad_payload = {
        "data": [
            {
                "starting_at": "2026-10-01T00:00:00Z",
                "results": [{"amount": "500", "currency": "gbp"}],
            }
        ]
    }
    with pytest.raises(UsageProbeError, match="Unsupported currency in Anthropic cost report: gbp"):
        parse_anthropic_cost_report_page(bad_payload)


def test_parse_anthropic_cost_report_page_invalid_payload() -> None:
    with pytest.raises(UsageProbeError, match="missing data list"):
        parse_anthropic_cost_report_page({})

    with pytest.raises(UsageProbeError, match="Invalid bucket"):
        parse_anthropic_cost_report_page({"data": ["not-a-dict"]})

    with pytest.raises(UsageProbeError, match="missing starting_at"):
        empty_bucket_payload: dict[str, object] = {"data": [cast(dict[str, object], {})]}
        parse_anthropic_cost_report_page(empty_bucket_payload)
