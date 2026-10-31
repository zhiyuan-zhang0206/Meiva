"""Pure formatting functions for usage metric cards."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from typing import TypeGuard

from meiva_builtins.plugins.meiva_usage.types import CardReading, RateWindow, UsageProbeError


def _is_obj_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_obj_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def format_dual_window_card(
    session: RateWindow | None,
    weekly: RateWindow | None,
    *,
    display_tz: tzinfo = UTC,
) -> CardReading:
    """Format dual window rate limits (e.g. 5h session and 7d weekly) into a card reading."""
    if session is None and weekly is None:
        raise UsageProbeError("Usage payload is missing rate-limit windows")

    windows: list[tuple[str, RateWindow, float]] = []
    if session is not None:
        rem = max(0.0, min(100.0, 100.0 - session.used_percent))
        windows.append(("5h", session, rem))
    if weekly is not None:
        rem = max(0.0, min(100.0, 100.0 - weekly.used_percent))
        windows.append(("7d", weekly, rem))

    tightest_rem = min(rem for _, _, rem in windows)
    value = f"{tightest_rem:.0f}% left"

    parts: list[str] = [f"{label} {rem:.0f}% left" for label, _, rem in windows]

    tightest_window = min(windows, key=lambda item: item[2])[1]
    reset_dt = tightest_window.resets_at
    if reset_dt is None:
        for _, window, _ in windows:
            if window.resets_at is not None:
                reset_dt = window.resets_at
                break

    if reset_dt is not None:
        aware_dt = _ensure_utc(reset_dt).astimezone(display_tz)
        parts.append(f"resets {aware_dt:%m-%d %H:%M}")

    detail = " · ".join(parts)
    status = "warn" if tightest_rem <= 10.0 else "ok"
    return CardReading(value=value, detail=detail, status=status)


def format_codex_window_card(
    window: RateWindow,
    *,
    display_tz: tzinfo = UTC,
) -> CardReading:
    """Format Codex primary rate limit window into a card reading."""
    remaining = max(0.0, min(100.0, 100.0 - window.used_percent))
    value = f"{remaining:.0f}% left"

    parts: list[str] = [f"used {window.used_percent:.0f}%"]

    minutes = window.window_minutes
    if minutes is not None:
        if minutes == 10080:
            window_label = "7d window"
        elif minutes == 1440:
            window_label = "1d window"
        elif minutes == 300:
            window_label = "5h window"
        elif minutes % 60 == 0:
            window_label = f"{minutes // 60}h window"
        else:
            window_label = f"{minutes}m window"
        parts.append(window_label)

    if window.resets_at is not None:
        aware_dt = _ensure_utc(window.resets_at).astimezone(display_tz)
        parts.append(f"resets {aware_dt:%m-%d %H:%M}")

    detail = " · ".join(parts)
    status = "warn" if remaining <= 10.0 else "ok"
    return CardReading(value=value, detail=detail, status=status)


def format_deepseek_balance_card(payload: Mapping[str, object]) -> CardReading:
    """Format DeepSeek balance response payload into a card reading."""
    raw_infos = payload.get("balance_infos")
    if not _is_obj_list(raw_infos) or len(raw_infos) == 0:
        raise UsageProbeError("DeepSeek payload missing balance_infos")

    chosen_entry: Mapping[str, object] | None = None
    for item in raw_infos:
        if _is_obj_mapping(item) and item.get("currency") == "CNY":
            chosen_entry = item
            break

    if chosen_entry is None:
        first = raw_infos[0]
        if _is_obj_mapping(first):
            chosen_entry = first

    if chosen_entry is None:
        raise UsageProbeError("DeepSeek payload contains invalid balance entries")

    currency = str(chosen_entry.get("currency", "")).strip()
    total = str(chosen_entry.get("total_balance", "")).strip()
    if not currency or not total:
        raise UsageProbeError("DeepSeek balance entry missing currency or total_balance")

    value = f"{currency} {total}"

    other_balances: list[str] = []
    for item in raw_infos:
        if _is_obj_mapping(item) and item is not chosen_entry:
            other_curr = str(item.get("currency", "")).strip()
            other_total = str(item.get("total_balance", "")).strip()
            if other_curr and other_total:
                other_balances.append(f"{other_curr} {other_total}")

    detail = " · ".join(other_balances) if other_balances else "platform.deepseek.com"

    status = "warn" if payload.get("is_available") is False else "ok"
    return CardReading(value=value, detail=detail, status=status)
