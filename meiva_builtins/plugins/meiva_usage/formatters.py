# meiva:note
# Pure formatting functions for usage metric cards.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Literal, TypeGuard

from meiva_builtins.plugins.meiva_usage.types import (
    CardReading,
    RateWindow,
    UsageProbeError,
    WindowPair,
)


def _is_obj_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_obj_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _find_dual_window_reset(windows: list[tuple[str, RateWindow, float]]) -> datetime | None:
    tightest_window = min(windows, key=lambda item: item[2])[1]
    if tightest_window.resets_at is not None:
        return tightest_window.resets_at
    for _, window, _ in windows:
        if window.resets_at is not None:
            return window.resets_at
    return None


def format_dual_window_card(
    session: RateWindow | None,
    weekly: RateWindow | None,
    *,
    display_tz: tzinfo = UTC,
) -> CardReading:
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

    reset_dt = _find_dual_window_reset(windows)
    if reset_dt is not None:
        aware_dt = _ensure_utc(reset_dt).astimezone(display_tz)
        parts.append(f"resets {aware_dt:%m-%d %H:%M}")

    detail = " · ".join(parts)
    status = "warn" if tightest_rem <= 10.0 else "ok"
    return CardReading(value=value, detail=detail, status=status)


def _format_antigravity_dual_window(
    session: RateWindow,
    weekly: RateWindow,
    *,
    display_tz: tzinfo,
) -> CardReading:
    weekly_rem = max(0.0, min(100.0, 100.0 - weekly.used_percent))
    session_rem = max(0.0, min(100.0, 100.0 - session.used_percent))
    value = f"7d {weekly_rem:.0f}% · 5h {session_rem:.0f}% left"
    reset_parts: list[str] = []
    if weekly.resets_at is not None:
        reset_parts.append(f"7d {_ensure_utc(weekly.resets_at).astimezone(display_tz):%m-%d %H:%M}")
    if session.resets_at is not None:
        reset_parts.append(
            f"5h {_ensure_utc(session.resets_at).astimezone(display_tz):%m-%d %H:%M}"
        )
    detail = f"resets {' · '.join(reset_parts)}" if reset_parts else "7d & 5h windows"
    status: Literal["ok", "warn"] = "warn" if min(weekly_rem, session_rem) <= 10.0 else "ok"
    return CardReading(value=value, detail=detail, status=status)


def _format_antigravity_single_window(
    window: RateWindow,
    *,
    is_weekly: bool,
    display_tz: tzinfo,
) -> CardReading:
    rem = max(0.0, min(100.0, 100.0 - window.used_percent))
    value = f"{rem:.0f}% left"
    parts = ["7d window" if is_weekly else "5h window"]
    if window.resets_at is not None:
        parts.append(f"resets {_ensure_utc(window.resets_at).astimezone(display_tz):%m-%d %H:%M}")
    detail = " · ".join(parts)
    status: Literal["ok", "warn"] = "warn" if rem <= 10.0 else "ok"
    return CardReading(value=value, detail=detail, status=status)


def format_antigravity_group_card(
    windows: WindowPair,
    *,
    display_tz: tzinfo = UTC,
) -> CardReading:
    if windows.session is None and windows.weekly is None:
        raise UsageProbeError("Antigravity quota group is missing rate-limit windows")
    if windows.weekly is not None and windows.session is not None:
        return _format_antigravity_dual_window(
            windows.session,
            windows.weekly,
            display_tz=display_tz,
        )
    if windows.weekly is not None:
        return _format_antigravity_single_window(
            windows.weekly,
            is_weekly=True,
            display_tz=display_tz,
        )
    if windows.session is not None:
        return _format_antigravity_single_window(
            windows.session,
            is_weekly=False,
            display_tz=display_tz,
        )
    raise UsageProbeError("Antigravity quota group is missing rate-limit windows")


def format_codex_window_card(
    window: RateWindow,
    *,
    display_tz: tzinfo = UTC,
) -> CardReading:
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


def _select_deepseek_balance_entry(
    infos: list[object],
) -> tuple[Mapping[str, object], list[Mapping[str, object]]]:
    mappings = [item for item in infos if _is_obj_mapping(item)]
    if not mappings:
        raise UsageProbeError("DeepSeek payload contains invalid balance entries")
    chosen = next((m for m in mappings if m.get("currency") == "CNY"), mappings[0])
    others = [m for m in mappings if m is not chosen]
    return chosen, others


def _format_other_balances(entries: list[Mapping[str, object]]) -> str:
    balances: list[str] = []
    for item in entries:
        curr = str(item.get("currency", "")).strip()
        total = str(item.get("total_balance", "")).strip()
        if curr and total:
            balances.append(f"{curr} {total}")
    return " · ".join(balances) if balances else "platform.deepseek.com"


def format_deepseek_balance_card(payload: Mapping[str, object]) -> CardReading:
    raw_infos = payload.get("balance_infos")
    if not _is_obj_list(raw_infos) or not raw_infos:
        raise UsageProbeError("DeepSeek payload missing balance_infos")

    chosen, others = _select_deepseek_balance_entry(raw_infos)
    currency = str(chosen.get("currency", "")).strip()
    total = str(chosen.get("total_balance", "")).strip()
    if not currency or not total:
        raise UsageProbeError("DeepSeek balance entry missing currency or total_balance")

    value = f"{currency} {total}"
    detail = _format_other_balances(others)
    status = "warn" if payload.get("is_available") is False else "ok"
    return CardReading(value=value, detail=detail, status=status)


def format_admin_cost_card(
    daily_usd: Mapping[date, Decimal],
    *,
    today_utc: date,
    known_balance_usd: float | None = None,
    balance_as_of: date | None = None,
) -> CardReading:
    if (known_balance_usd is None) != (balance_as_of is None):
        raise ValueError("known_balance_usd and balance_as_of must be configured together")

    today_spend = daily_usd.get(today_utc, Decimal("0"))

    if known_balance_usd is None or balance_as_of is None:
        cutoff = today_utc - timedelta(days=29)
        spend_30d = sum(
            (amt for day, amt in daily_usd.items() if cutoff <= day <= today_utc),
            Decimal("0"),
        )
        value = f"${spend_30d:.2f} (30d)"
        detail = f"today ${today_spend:.2f} · 30d ${spend_30d:.2f}"
        return CardReading(value=value, detail=detail, status="ok")

    baseline = Decimal(str(known_balance_usd))
    spent_since = sum(
        (amt for day, amt in daily_usd.items() if day >= balance_as_of),
        Decimal("0"),
    )
    remaining = baseline - spent_since
    value = f"${remaining:.2f} left"
    detail = (
        f"today ${today_spend:.2f} · spent ${spent_since:.2f} since {balance_as_of.isoformat()}"
    )
    warn_threshold = max(Decimal("5"), baseline * Decimal("0.10"))
    status: Literal["ok", "warn"] = "warn" if remaining <= warn_threshold else "ok"
    return CardReading(value=value, detail=detail, status=status)
