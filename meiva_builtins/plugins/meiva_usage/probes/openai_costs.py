# meiva:note
# Probe for OpenAI organization admin costs.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path

from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.formatters import format_admin_cost_card
from meiva_builtins.plugins.meiva_usage.parsers import parse_openai_costs_page
from meiva_builtins.plugins.meiva_usage.probes.transport import (
    HttpJsonFetcher,
    HttpJsonRequest,
    HttpStatusError,
)
from meiva_builtins.plugins.meiva_usage.types import CardReading, UsageProbeError

MAX_COST_PAGES = 20


def _build_openai_url(start_epoch: int, next_page: str | None) -> str:
    url = (
        f"https://api.openai.com/v1/organization/costs"
        f"?start_time={start_epoch}&bucket_width=1d&limit=180"
    )
    if next_page:
        url += f"&page={urllib.parse.quote(next_page, safe='')}"
    return url


def _compute_start_day(today_utc: date, balance_as_of: str | None) -> date:
    if balance_as_of is not None:
        return min(date.fromisoformat(balance_as_of), today_utc - timedelta(days=29))
    return today_utc - timedelta(days=29)


def _handle_openai_http_error(exc: HttpStatusError, env_name: str) -> UsageProbeError:
    if exc.status_code == 401:
        return UsageProbeError(f"OpenAI Admin key in {env_name} is invalid (401)")
    if exc.status_code == 403:
        return UsageProbeError(
            f"OpenAI key in {env_name} lacks api.usage.read / Admin permission (403)"
        )
    return UsageProbeError(f"OpenAI costs request failed with HTTP {exc.status_code}")


def probe_openai(
    collector: CollectorConfig,
    *,
    user_home: Path,
    environ: Mapping[str, str],
    now: datetime,
    platform: str,
    http_fetch: HttpJsonFetcher,
    display_tz: tzinfo = UTC,
) -> CardReading:
    del user_home, platform, display_tz
    env_name = collector.key_env or ""
    api_key = environ.get(env_name, "").strip()
    if not api_key:
        raise UsageProbeError(f"Environment variable {env_name} is not set")

    today_utc = now.astimezone(UTC).date()
    start_day = _compute_start_day(today_utc, collector.balance_as_of)
    start_epoch = int(
        datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC).timestamp()
    )

    daily_usd: dict[date, Decimal] = {}
    next_page: str | None = None
    page_count = 0

    while True:
        url = _build_openai_url(start_epoch, next_page)
        req = HttpJsonRequest(
            url=url,
            method="GET",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        try:
            payload = http_fetch(req)
        except HttpStatusError as exc:
            raise _handle_openai_http_error(exc, env_name) from None

        page = parse_openai_costs_page(payload)
        for day, amt in page.daily_usd.items():
            daily_usd[day] = daily_usd.get(day, Decimal("0")) + amt

        page_count += 1
        if page.next_page is None:
            break
        if page_count >= MAX_COST_PAGES:
            raise UsageProbeError("OpenAI costs pagination exceeded 20 pages")
        next_page = page.next_page

    balance_as_of_date = (
        date.fromisoformat(collector.balance_as_of) if collector.balance_as_of is not None else None
    )
    return format_admin_cost_card(
        daily_usd,
        today_utc=today_utc,
        known_balance_usd=collector.known_balance_usd,
        balance_as_of=balance_as_of_date,
    )
