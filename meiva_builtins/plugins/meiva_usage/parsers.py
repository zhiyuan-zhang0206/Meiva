# meiva:note
# Pure payload and credential parsing functions for meiva_usage.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TypeGuard

from meiva_builtins.plugins.meiva_usage.types import (
    CostBucketsPage,
    RateWindow,
    UsageProbeError,
    parse_timestamp,
    parse_timestamp_opt,
)

__all__ = (
    "parse_anthropic_cost_report_page",
    "parse_antigravity_windows",
    "parse_claude_windows",
    "parse_codex_primary_window",
    "parse_openai_costs_page",
    "sanitize_codex_rpc_error",
)


def _is_mapping(val: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(val, Mapping)


def _is_list(val: object) -> TypeGuard[list[object]]:
    return isinstance(val, list)


def _parse_claude_limit_item(
    item: object,
) -> tuple[str, float, datetime | None] | None:
    if not _is_mapping(item):
        return None
    kind = str(item.get("kind", "")).lower()
    raw_percent = (
        item.get("percent") if item.get("percent") is not None else item.get("used_percent")
    )
    if raw_percent is None:
        return None
    try:
        pct = float(str(raw_percent))
    except (ValueError, TypeError):
        return None
    resets_at = parse_timestamp_opt(item.get("resets_at"))
    return kind, pct, resets_at


def _parse_legacy_claude_window(window_obj: object, minutes: int) -> RateWindow | None:
    if not _is_mapping(window_obj):
        return None
    util = window_obj.get("utilization")
    if util is None:
        return None
    return RateWindow(
        used_percent=float(str(util)),
        resets_at=parse_timestamp_opt(window_obj.get("resets_at")),
        window_minutes=minutes,
    )


def parse_claude_windows(
    payload: Mapping[str, object],
) -> tuple[RateWindow | None, RateWindow | None]:
    session_win: RateWindow | None = None
    weekly_win: RateWindow | None = None

    limits = payload.get("limits")
    if _is_list(limits):
        for item in limits:
            parsed = _parse_claude_limit_item(item)
            if parsed is None:
                continue
            kind, pct, resets_at = parsed
            if kind == "session" and session_win is None:
                session_win = RateWindow(used_percent=pct, resets_at=resets_at, window_minutes=300)
            elif kind in ("weekly_all", "weekly") and weekly_win is None:
                weekly_win = RateWindow(used_percent=pct, resets_at=resets_at, window_minutes=10080)

    if session_win is None:
        session_win = _parse_legacy_claude_window(payload.get("five_hour"), 300)
    if weekly_win is None:
        weekly_win = _parse_legacy_claude_window(payload.get("seven_day"), 10080)

    return session_win, weekly_win


def parse_codex_primary_window(payload: Mapping[str, object]) -> RateWindow:
    candidate: Mapping[str, object] | None = None
    rate_limits = payload.get("rateLimits")
    if _is_mapping(rate_limits):
        primary = rate_limits.get("primary")
        if _is_mapping(primary):
            candidate = primary

    if candidate is None:
        limits_by_id = payload.get("rateLimitsByLimitId")
        if _is_mapping(limits_by_id):
            codex_limits = limits_by_id.get("codex")
            if _is_mapping(codex_limits):
                primary = codex_limits.get("primary")
                if _is_mapping(primary):
                    candidate = primary

    if candidate is None and "usedPercent" in payload:
        candidate = payload

    if candidate is None:
        raise UsageProbeError("Codex payload missing primary rate limit window")

    used_pct_raw = candidate.get("usedPercent")
    if used_pct_raw is None:
        raise UsageProbeError("Codex primary window missing usedPercent")
    used_percent = float(str(used_pct_raw))

    window_mins = candidate.get("windowDurationMins")
    window_minutes = int(str(window_mins)) if window_mins is not None else None

    resets_at = parse_timestamp_opt(candidate.get("resetsAt"))
    return RateWindow(
        used_percent=used_percent,
        resets_at=resets_at,
        window_minutes=window_minutes,
    )


def _match_known_rpc_error(combined: str, code: int | None, message: str) -> str | None:
    if "token_revoked" in combined or "token revoked" in combined:
        return "Session expired (token_revoked); sign in again"
    if "unauthorized" in combined or code in (401, -32401) or "401" in message:
        return "Unauthorized (401); sign in again"
    if "forbidden" in combined or code in (403, -32403) or "403" in message:
        return "Forbidden (403); check permissions"
    if "rate_limit" in combined or "rate limit" in combined or code == 429 or "429" in message:
        return "Rate limit exceeded; retry later"
    return None


def sanitize_codex_rpc_error(error_payload: object) -> str:
    code: int | None = None
    message = ""
    data_str = ""

    if _is_mapping(error_payload):
        code_val = error_payload.get("code")
        if isinstance(code_val, int):
            code = code_val
        message = str(error_payload.get("message", ""))
        data_str = str(error_payload.get("data", ""))
    elif isinstance(error_payload, str):
        message = error_payload

    known = _match_known_rpc_error(f"{message} {data_str}".lower(), code, message)
    if known is not None:
        return known

    clean = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", message)
    clean = re.sub(r"[a-f0-9]{32,}", "[REDACTED]", clean)
    clean = clean.strip()
    if not clean:
        return f"Codex RPC error ({code})" if code is not None else "Codex RPC error"
    if len(clean) > 80:
        clean = clean[:77] + "..."
    return clean


def _is_gemini_group(group: Mapping[str, object]) -> bool:
    name = str(
        group.get("displayName", "") or group.get("name", "") or group.get("groupId", "")
    ).lower()
    if "gemini" in name:
        return True
    buckets = group.get("buckets")
    if _is_list(buckets):
        for b in buckets:
            if _is_mapping(b):
                bucket_id = str(b.get("bucketId", "")).lower()
                if bucket_id.startswith("gemini-"):
                    return True
    return False


def _select_antigravity_buckets(payload: Mapping[str, object]) -> list[object] | None:
    resp = payload.get("response")
    root = resp if _is_mapping(resp) else payload
    groups = root.get("groups")
    chosen_group: Mapping[str, object] | None = None

    if _is_list(groups) and groups:
        for group in groups:
            if not _is_mapping(group):
                continue
            if _is_gemini_group(group):
                chosen_group = group
                break
        if chosen_group is None:
            first = groups[0]
            if _is_mapping(first):
                chosen_group = first

    buckets: object = (
        chosen_group.get("buckets") if chosen_group is not None else root.get("buckets")
    )
    if not _is_list(buckets):
        return None
    return buckets


def _calc_antigravity_bucket_used(bucket: Mapping[str, object]) -> float:
    if bucket.get("disabled") is True:
        return 100.0
    rem_frac = bucket.get("remainingFraction")
    rem_amt = bucket.get("remainingAmount")
    limit = bucket.get("limit")

    if rem_frac is not None:
        rem_ratio = float(str(rem_frac))
    elif rem_amt is not None and limit is not None and float(str(limit)) > 0:
        rem_ratio = float(str(rem_amt)) / float(str(limit))
    elif rem_frac is None and rem_amt is None:
        rem_ratio = 0.0
    else:
        rem_ratio = 1.0

    return round(max(0.0, min(100.0, (1.0 - rem_ratio) * 100.0)), 4)


def _classify_antigravity_window(window_id: str) -> str | None:
    if window_id == "5h" or window_id.endswith(("-5h", "5h")):
        return "5h"
    if window_id == "weekly" or window_id.endswith(("-weekly", "weekly")) or "7d" in window_id:
        return "weekly"
    return None


def _parse_antigravity_bucket(
    bucket: object,
) -> tuple[str, RateWindow] | None:
    if not _is_mapping(bucket):
        return None
    raw_id = (
        bucket.get("bucketId")
        or bucket.get("window")
        or bucket.get("windowId")
        or bucket.get("name")
        or ""
    )
    kind = _classify_antigravity_window(str(raw_id).lower())
    if kind is None:
        return None
    used_percent = _calc_antigravity_bucket_used(bucket)
    reset_raw = bucket.get("resetTime") or bucket.get("resetsAt") or bucket.get("reset_time")
    reset_dt = parse_timestamp_opt(reset_raw)
    minutes = 300 if kind == "5h" else 10080
    return kind, RateWindow(used_percent=used_percent, resets_at=reset_dt, window_minutes=minutes)


def parse_antigravity_windows(
    payload: Mapping[str, object],
) -> tuple[RateWindow | None, RateWindow | None]:
    buckets_list = _select_antigravity_buckets(payload)
    if buckets_list is None:
        return None, None

    session_win: RateWindow | None = None
    weekly_win: RateWindow | None = None

    for bucket in buckets_list:
        parsed = _parse_antigravity_bucket(bucket)
        if parsed is None:
            continue
        kind, win = parsed
        if kind == "5h" and session_win is None:
            session_win = win
        elif kind == "weekly" and weekly_win is None:
            weekly_win = win

    return session_win, weekly_win


def _parse_pagination_next_page(payload: Mapping[str, object], provider_name: str) -> str | None:
    if payload.get("has_more") is True:
        next_page = payload.get("next_page")
        if isinstance(next_page, str) and next_page.strip():
            return next_page.strip()
        raise UsageProbeError(f"{provider_name} indicated has_more but next_page is missing")
    return None


def _parse_openai_amount(result: object) -> Decimal:
    if not _is_mapping(result):
        raise UsageProbeError("Invalid result in OpenAI cost bucket")
    amount_obj = result.get("amount")
    if not _is_mapping(amount_obj):
        raise UsageProbeError("OpenAI cost result missing amount object")
    val_raw = amount_obj.get("value")
    if val_raw is None:
        raise UsageProbeError("OpenAI cost amount missing value")
    try:
        amt = Decimal(str(val_raw))
    except Exception as exc:
        raise UsageProbeError(f"Invalid OpenAI cost amount value: {val_raw!r}") from exc
    currency = str(amount_obj.get("currency", "")).strip().lower()
    if amt > Decimal("0") and currency and currency != "usd":
        raise UsageProbeError(f"Unsupported currency in OpenAI costs: {currency}")
    return amt


def _parse_openai_bucket(bucket: object, daily_usd: dict[date, Decimal]) -> None:
    if not _is_mapping(bucket):
        raise UsageProbeError("Invalid bucket in OpenAI costs data")
    start_time_raw = bucket.get("start_time")
    if not isinstance(start_time_raw, (int, float)) or isinstance(start_time_raw, bool):
        raise UsageProbeError("OpenAI cost bucket missing valid start_time")
    bucket_date = datetime.fromtimestamp(float(start_time_raw), tz=UTC).date()
    results = bucket.get("results")
    if not _is_list(results):
        raise UsageProbeError("OpenAI cost bucket missing results list")
    for result in results:
        daily_usd[bucket_date] = daily_usd.get(bucket_date, Decimal("0")) + _parse_openai_amount(
            result
        )


def parse_openai_costs_page(payload: Mapping[str, object]) -> CostBucketsPage:
    data = payload.get("data")
    if not _is_list(data):
        raise UsageProbeError("OpenAI costs payload missing data list")
    daily_usd: dict[date, Decimal] = {}
    for bucket in data:
        _parse_openai_bucket(bucket, daily_usd)
    next_page = _parse_pagination_next_page(payload, "OpenAI costs")
    return CostBucketsPage(daily_usd=daily_usd, next_page=next_page)


def _parse_anthropic_amount(result: object) -> Decimal:
    if not _is_mapping(result):
        raise UsageProbeError("Invalid result in Anthropic cost report bucket")
    amount_raw = result.get("amount")
    if amount_raw is None:
        raise UsageProbeError("Anthropic cost result missing amount")
    try:
        cents = Decimal(str(amount_raw))
    except Exception as exc:
        raise UsageProbeError(f"Invalid Anthropic cost amount value: {amount_raw!r}") from exc
    currency = str(result.get("currency", "")).strip().lower()
    dollars = cents / Decimal(100)
    if dollars > Decimal("0") and currency and currency != "usd":
        raise UsageProbeError(f"Unsupported currency in Anthropic cost report: {currency}")
    return dollars


def _parse_anthropic_bucket(bucket: object, daily_usd: dict[date, Decimal]) -> None:
    if not _is_mapping(bucket):
        raise UsageProbeError("Invalid bucket in Anthropic cost report data")
    starting_at_raw = bucket.get("starting_at")
    if not isinstance(starting_at_raw, str) or not starting_at_raw.strip():
        raise UsageProbeError("Anthropic cost bucket missing starting_at")
    bucket_date = parse_timestamp(starting_at_raw).date()
    results = bucket.get("results")
    if not _is_list(results):
        raise UsageProbeError("Anthropic cost bucket missing results list")
    for result in results:
        daily_usd[bucket_date] = daily_usd.get(bucket_date, Decimal("0")) + _parse_anthropic_amount(
            result
        )


def parse_anthropic_cost_report_page(payload: Mapping[str, object]) -> CostBucketsPage:
    data = payload.get("data")
    if not _is_list(data):
        raise UsageProbeError("Anthropic cost report missing data list")
    daily_usd: dict[date, Decimal] = {}
    for bucket in data:
        _parse_anthropic_bucket(bucket, daily_usd)
    next_page = _parse_pagination_next_page(payload, "Anthropic cost report")
    return CostBucketsPage(daily_usd=daily_usd, next_page=next_page)
