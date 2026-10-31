"""Probe for DeepSeek account balance."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.formatters import format_deepseek_balance_card
from meiva_builtins.plugins.meiva_usage.probes.transport import (
    HttpJsonFetcher,
    HttpJsonRequest,
    HttpStatusError,
)
from meiva_builtins.plugins.meiva_usage.types import CardReading, UsageProbeError


def probe_deepseek(
    collector: CollectorConfig,
    *,
    user_home: Path,
    environ: Mapping[str, str],
    now: datetime,
    platform: str,
    http_fetch: HttpJsonFetcher,
    display_tz: tzinfo = UTC,
) -> CardReading:
    """Probe DeepSeek user balance via balance endpoint."""
    del user_home, now, platform, display_tz
    env_name = collector.key_env or ""
    api_key = environ.get(env_name, "").strip()
    if not api_key:
        raise UsageProbeError(f"Environment variable {env_name} is not set")

    req = HttpJsonRequest(
        url="https://api.deepseek.com/user/balance",
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )

    try:
        payload = http_fetch(req)
    except HttpStatusError as exc:
        if exc.status_code == 401:
            raise UsageProbeError(f"DeepSeek API key in {env_name} is invalid (401)") from None
        raise UsageProbeError(
            f"DeepSeek balance request failed with HTTP {exc.status_code}"
        ) from None

    return format_deepseek_balance_card(payload)
