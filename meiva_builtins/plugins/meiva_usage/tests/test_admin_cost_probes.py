from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.probes import (
    HttpJsonRequest,
    HttpStatusError,
    probe_anthropic,
    probe_collector,
    probe_openai,
)
from meiva_builtins.plugins.meiva_usage.types import UsageProbeError


def test_probe_openai_success_trailing_30d() -> None:
    collector = CollectorConfig(
        id="openai-30d",
        label="OpenAI",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_ADMIN_KEY",
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    today_epoch = int(datetime(2026, 11, 1, 0, 0, tzinfo=UTC).timestamp())
    start_30d_epoch = int(datetime(2026, 10, 3, 0, 0, tzinfo=UTC).timestamp())

    requests_seen: list[HttpJsonRequest] = []

    def mock_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        requests_seen.append(req)
        assert f"start_time={start_30d_epoch}" in req.url
        assert "bucket_width=1d" in req.url
        assert "limit=180" in req.url
        assert req.headers.get("Authorization") == "Bearer sk-admin-openai"
        assert req.headers.get("Accept") == "application/json"
        return {
            "data": [
                {
                    "start_time": today_epoch,
                    "results": [{"amount": {"value": 3.50, "currency": "usd"}}],
                },
                {
                    "start_time": start_30d_epoch,
                    "results": [{"amount": {"value": 11.50, "currency": "usd"}}],
                },
            ],
            "has_more": False,
        }

    reading = probe_openai(
        collector,
        user_home=Path("/nonexistent"),
        environ={"OPENAI_ADMIN_KEY": "sk-admin-openai"},
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    assert len(requests_seen) == 1
    assert reading.value == "$15.00 (30d)"
    assert reading.detail == "today $3.50 · 30d $15.00"
    assert reading.status == "ok"


def test_probe_openai_success_prepaid_balance() -> None:
    collector = CollectorConfig(
        id="openai-prepaid",
        label="OpenAI",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_ADMIN_KEY",
        known_balance_usd=100.0,
        balance_as_of="2026-10-15",
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    today_epoch = int(datetime(2026, 11, 1, 0, 0, tzinfo=UTC).timestamp())
    as_of_epoch = int(datetime(2026, 10, 15, 0, 0, tzinfo=UTC).timestamp())

    def mock_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        return {
            "data": [
                {
                    "start_time": today_epoch,
                    "results": [{"amount": {"value": 5.0, "currency": "usd"}}],
                },
                {
                    "start_time": as_of_epoch,
                    "results": [{"amount": {"value": 15.0, "currency": "usd"}}],
                },
            ],
            "has_more": False,
        }

    reading = probe_openai(
        collector,
        user_home=Path("/nonexistent"),
        environ={"OPENAI_ADMIN_KEY": "sk-admin-openai"},
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    assert reading.value == "$80.00 left"
    assert reading.detail == "today $5.00 · spent $20.00 since 2026-10-15"
    assert reading.status == "ok"


def test_probe_openai_multi_page_pagination() -> None:
    collector = CollectorConfig(
        id="openai-pages",
        label="OpenAI",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_ADMIN_KEY",
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    today_epoch = int(datetime(2026, 11, 1, 0, 0, tzinfo=UTC).timestamp())

    pages_called: list[str] = []

    def mock_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        pages_called.append(req.url)
        if len(pages_called) == 1:
            assert "page=" not in req.url
            return {
                "data": [
                    {
                        "start_time": today_epoch,
                        "results": [{"amount": {"value": 4.0, "currency": "usd"}}],
                    }
                ],
                "has_more": True,
                "next_page": "cursor_2",
            }
        assert "page=cursor_2" in req.url
        return {
            "data": [
                {
                    "start_time": today_epoch,
                    "results": [{"amount": {"value": 6.0, "currency": "usd"}}],
                }
            ],
            "has_more": False,
        }

    reading = probe_openai(
        collector,
        user_home=Path("/nonexistent"),
        environ={"OPENAI_ADMIN_KEY": "sk-admin-openai"},
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    assert len(pages_called) == 2
    assert reading.value == "$10.00 (30d)"
    assert reading.detail == "today $10.00 · 30d $10.00"


def test_probe_openai_exceed_max_pages_raises() -> None:
    collector = CollectorConfig(
        id="openai-inf",
        label="OpenAI",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_KEY",
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def mock_fetch(_req: HttpJsonRequest) -> dict[str, Any]:
        return {
            "data": [],
            "has_more": True,
            "next_page": "loop",
        }

    with pytest.raises(UsageProbeError, match="OpenAI costs pagination exceeded 20 pages"):
        probe_openai(
            collector,
            user_home=Path("/nonexistent"),
            environ={"OPENAI_KEY": "key"},
            now=now,
            platform="darwin",
            http_fetch=mock_fetch,
        )


def test_probe_openai_missing_key_env_raises() -> None:
    collector = CollectorConfig(
        id="openai-missing-env",
        label="OpenAI",
        provider="openai",
        machine="host-a",
        key_env="MISSING_OAI_KEY",
    )
    with pytest.raises(UsageProbeError, match="Environment variable MISSING_OAI_KEY is not set"):
        probe_openai(
            collector,
            user_home=Path("/nonexistent"),
            environ={},
            now=datetime.now(UTC),
            platform="darwin",
            http_fetch=lambda _req: {},
        )


def test_probe_openai_http_errors() -> None:
    collector = CollectorConfig(
        id="openai-err",
        label="OpenAI",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_ADMIN_KEY",
    )
    now = datetime.now(UTC)

    def mock_401(_req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(401, "Invalid API key")

    with pytest.raises(
        UsageProbeError, match=r"OpenAI Admin key in OPENAI_ADMIN_KEY is invalid \(401\)"
    ):
        probe_openai(
            collector,
            user_home=Path("/nonexistent"),
            environ={"OPENAI_ADMIN_KEY": "bad"},
            now=now,
            platform="darwin",
            http_fetch=mock_401,
        )

    def mock_403(_req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(403, "Missing scope")

    with pytest.raises(
        UsageProbeError,
        match=r"OpenAI key in OPENAI_ADMIN_KEY lacks api\.usage\.read / Admin permission \(403\)",
    ):
        probe_openai(
            collector,
            user_home=Path("/nonexistent"),
            environ={"OPENAI_ADMIN_KEY": "bad"},
            now=now,
            platform="darwin",
            http_fetch=mock_403,
        )

    def mock_500(_req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(500, "Internal Server Error")

    with pytest.raises(UsageProbeError, match="OpenAI costs request failed with HTTP 500"):
        probe_openai(
            collector,
            user_home=Path("/nonexistent"),
            environ={"OPENAI_ADMIN_KEY": "bad"},
            now=now,
            platform="darwin",
            http_fetch=mock_500,
        )


def test_probe_anthropic_success_trailing_30d() -> None:
    collector = CollectorConfig(
        id="anthropic-30d",
        label="Anthropic",
        provider="anthropic",
        machine="host-a",
        key_env="ANTHROPIC_ADMIN_KEY",
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    requests_seen: list[HttpJsonRequest] = []

    def mock_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        requests_seen.append(req)
        assert "starting_at=2026-10-03T00:00:00Z" in req.url
        assert "bucket_width=1d" in req.url
        assert "limit=31" in req.url
        assert req.headers.get("x-api-key") == "sk-ant-admin-123"
        assert req.headers.get("anthropic-version") == "2023-06-01"
        assert req.headers.get("Accept") == "application/json"
        return {
            "data": [
                {
                    "starting_at": "2026-11-01T00:00:00Z",
                    "results": [{"amount": "250", "currency": "USD"}],
                },
                {
                    "starting_at": "2026-10-03T00:00:00Z",
                    "results": [{"amount": "1750", "currency": "USD"}],
                },
            ],
            "has_more": False,
        }

    reading = probe_anthropic(
        collector,
        user_home=Path("/nonexistent"),
        environ={"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-123"},
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    assert len(requests_seen) == 1
    assert reading.value == "$20.00 (30d)"
    assert reading.detail == "today $2.50 · 30d $20.00"
    assert reading.status == "ok"


def test_probe_anthropic_success_prepaid_balance() -> None:
    collector = CollectorConfig(
        id="anthropic-prepaid",
        label="Anthropic",
        provider="anthropic",
        machine="host-a",
        key_env="ANTHROPIC_ADMIN_KEY",
        known_balance_usd=50.0,
        balance_as_of="2026-09-01",
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def mock_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        assert "starting_at=2026-09-01T00:00:00Z" in req.url
        return {
            "data": [
                {
                    "starting_at": "2026-11-01T00:00:00Z",
                    "results": [{"amount": "500", "currency": "USD"}],
                },
                {
                    "starting_at": "2026-09-15T00:00:00Z",
                    "results": [{"amount": "4000", "currency": "USD"}],
                },
            ],
            "has_more": False,
        }

    reading = probe_anthropic(
        collector,
        user_home=Path("/nonexistent"),
        environ={"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-123"},
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    assert reading.value == "$5.00 left"
    assert reading.detail == "today $5.00 · spent $45.00 since 2026-09-01"
    assert reading.status == "warn"


def test_probe_anthropic_multi_page_pagination() -> None:
    collector = CollectorConfig(
        id="ant-pages",
        label="Anthropic",
        provider="anthropic",
        machine="host-a",
        key_env="ANTHROPIC_ADMIN_KEY",
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    pages: list[str] = []

    def mock_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        pages.append(req.url)
        if len(pages) == 1:
            assert "page=" not in req.url
            return {
                "data": [
                    {
                        "starting_at": "2026-11-01T00:00:00Z",
                        "results": [{"amount": "100", "currency": "USD"}],
                    }
                ],
                "has_more": True,
                "next_page": "ant_cursor_2",
            }
        assert "page=ant_cursor_2" in req.url
        return {
            "data": [
                {
                    "starting_at": "2026-11-01T00:00:00Z",
                    "results": [{"amount": "200", "currency": "USD"}],
                }
            ],
            "has_more": False,
        }

    reading = probe_anthropic(
        collector,
        user_home=Path("/nonexistent"),
        environ={"ANTHROPIC_ADMIN_KEY": "key"},
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    assert len(pages) == 2
    assert reading.value == "$3.00 (30d)"


def test_probe_anthropic_exceed_max_pages_raises() -> None:
    collector = CollectorConfig(
        id="ant-inf",
        label="Anthropic",
        provider="anthropic",
        machine="host-a",
        key_env="KEY",
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def mock_fetch(_req: HttpJsonRequest) -> dict[str, Any]:
        return {"data": [], "has_more": True, "next_page": "loop"}

    with pytest.raises(UsageProbeError, match="Anthropic cost_report pagination exceeded 20 pages"):
        probe_anthropic(
            collector,
            user_home=Path("/nonexistent"),
            environ={"KEY": "k"},
            now=now,
            platform="darwin",
            http_fetch=mock_fetch,
        )


def test_probe_anthropic_missing_key_env_raises() -> None:
    collector = CollectorConfig(
        id="ant-missing-env",
        label="Anthropic",
        provider="anthropic",
        machine="host-a",
        key_env="MISSING_ANT_KEY",
    )
    with pytest.raises(UsageProbeError, match="Environment variable MISSING_ANT_KEY is not set"):
        probe_anthropic(
            collector,
            user_home=Path("/nonexistent"),
            environ={},
            now=datetime.now(UTC),
            platform="darwin",
            http_fetch=lambda _req: {},
        )


def test_probe_anthropic_http_errors() -> None:
    collector = CollectorConfig(
        id="ant-err",
        label="Anthropic",
        provider="anthropic",
        machine="host-a",
        key_env="ANTHROPIC_ADMIN_KEY",
    )
    now = datetime.now(UTC)

    def mock_401(_req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(401, "Invalid key")

    with pytest.raises(
        UsageProbeError, match=r"Anthropic Admin key in ANTHROPIC_ADMIN_KEY is invalid \(401\)"
    ):
        probe_anthropic(
            collector,
            user_home=Path("/nonexistent"),
            environ={"ANTHROPIC_ADMIN_KEY": "bad"},
            now=now,
            platform="darwin",
            http_fetch=mock_401,
        )

    def mock_403(_req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(403, "Forbidden")

    with pytest.raises(
        UsageProbeError,
        match="Anthropic key in ANTHROPIC_ADMIN_KEY lacks Admin cost_report permission",
    ):
        probe_anthropic(
            collector,
            user_home=Path("/nonexistent"),
            environ={"ANTHROPIC_ADMIN_KEY": "bad"},
            now=now,
            platform="darwin",
            http_fetch=mock_403,
        )

    def mock_502(_req: HttpJsonRequest) -> dict[str, Any]:
        raise HttpStatusError(502, "Bad Gateway")

    with pytest.raises(UsageProbeError, match="Anthropic cost_report request failed with HTTP 502"):
        probe_anthropic(
            collector,
            user_home=Path("/nonexistent"),
            environ={"ANTHROPIC_ADMIN_KEY": "bad"},
            now=now,
            platform="darwin",
            http_fetch=mock_502,
        )


def test_probe_collector_dispatcher_openai_and_anthropic() -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    today_epoch = int(datetime(2026, 11, 1, 0, 0, tzinfo=UTC).timestamp())

    c_oai = CollectorConfig(
        id="oai",
        label="OpenAI",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_KEY",
    )
    r_oai = probe_collector(
        c_oai,
        user_home=Path("/nonexistent"),
        environ={"OPENAI_KEY": "key"},
        now=now,
        platform="darwin",
        http_fetch=lambda _req: {
            "data": [
                {
                    "start_time": today_epoch,
                    "results": [{"amount": {"value": 1.25, "currency": "usd"}}],
                }
            ],
            "has_more": False,
        },
    )
    assert r_oai["oai"].value == "$1.25 (30d)"

    c_ant = CollectorConfig(
        id="ant",
        label="Anthropic",
        provider="anthropic",
        machine="host-a",
        key_env="ANTHROPIC_KEY",
    )
    r_ant = probe_collector(
        c_ant,
        user_home=Path("/nonexistent"),
        environ={"ANTHROPIC_KEY": "key"},
        now=now,
        platform="darwin",
        http_fetch=lambda _req: {
            "data": [
                {
                    "starting_at": "2026-11-01T00:00:00Z",
                    "results": [{"amount": "250", "currency": "USD"}],
                }
            ],
            "has_more": False,
        },
    )
    assert r_ant["ant"].value == "$2.50 (30d)"
