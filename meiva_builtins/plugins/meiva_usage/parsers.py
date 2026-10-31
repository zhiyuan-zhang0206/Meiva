"""Pure payload and credential parsing functions for meiva_usage."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypeGuard

from meiva_builtins.plugins.meiva_usage.types import (
    AntigravityCredentials,
    ClaudeOAuthCredentials,
    RateWindow,
    UsageProbeError,
    email_from_jwt,
    parse_timestamp,
    parse_timestamp_opt,
)


def _is_mapping(val: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(val, Mapping)


def _is_list(val: object) -> TypeGuard[list[object]]:
    return isinstance(val, list)


# ---------------------------------------------------------------------------
# Claude Parsers
# ---------------------------------------------------------------------------


def parse_claude_oauth_blob(data: Mapping[str, object] | str) -> ClaudeOAuthCredentials:
    """Parse Claude Code OAuth credentials from a JSON string or mapping."""
    parsed: Mapping[str, object]
    if isinstance(data, str):
        try:
            raw: object = json.loads(data)
        except Exception as exc:
            raise UsageProbeError("Invalid Claude credentials JSON") from exc
        if not _is_mapping(raw):
            raise UsageProbeError("Invalid Claude credentials format")
        parsed = raw
    elif _is_mapping(data):
        parsed = data
    else:
        raise UsageProbeError("Invalid Claude credentials format")

    oauth_raw = parsed.get("claudeAiOauth")
    oauth_obj: Mapping[str, object] = oauth_raw if _is_mapping(oauth_raw) else parsed

    token = oauth_obj.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise UsageProbeError("Claude credentials missing claudeAiOauth.accessToken")

    expires_raw = oauth_obj.get("expiresAt")
    if expires_raw is not None:
        try:
            dt = parse_timestamp(expires_raw)
        except Exception:
            dt = datetime.fromtimestamp(0, tz=UTC)
    else:
        dt = datetime.fromtimestamp(0, tz=UTC)

    return ClaudeOAuthCredentials(access_token=token.strip(), expires_at=dt)


def parse_claude_windows(
    payload: Mapping[str, object],
) -> tuple[RateWindow | None, RateWindow | None]:
    """Parse Claude rate limit windows from modern limits array or legacy fields."""
    session_win: RateWindow | None = None
    weekly_win: RateWindow | None = None

    limits = payload.get("limits")
    if _is_list(limits):
        for item in limits:
            if not _is_mapping(item):
                continue
            kind = str(item.get("kind", "")).lower()
            raw_percent = (
                item.get("percent") if item.get("percent") is not None else item.get("used_percent")
            )
            if raw_percent is None:
                continue
            try:
                pct = float(str(raw_percent))
            except (ValueError, TypeError):
                continue
            resets_at = parse_timestamp_opt(item.get("resets_at"))
            if kind == "session" and session_win is None:
                session_win = RateWindow(used_percent=pct, resets_at=resets_at, window_minutes=300)
            elif kind in ("weekly_all", "weekly") and weekly_win is None:
                weekly_win = RateWindow(used_percent=pct, resets_at=resets_at, window_minutes=10080)

    five_hour = payload.get("five_hour")
    if session_win is None and _is_mapping(five_hour):
        util = five_hour.get("utilization")
        if util is not None:
            session_win = RateWindow(
                used_percent=float(str(util)),
                resets_at=parse_timestamp_opt(five_hour.get("resets_at")),
                window_minutes=300,
            )

    seven_day = payload.get("seven_day")
    if weekly_win is None and _is_mapping(seven_day):
        util = seven_day.get("utilization")
        if util is not None:
            weekly_win = RateWindow(
                used_percent=float(str(util)),
                resets_at=parse_timestamp_opt(seven_day.get("resets_at")),
                window_minutes=10080,
            )

    return session_win, weekly_win


# ---------------------------------------------------------------------------
# Codex Parsers
# ---------------------------------------------------------------------------


def parse_codex_primary_window(payload: Mapping[str, object]) -> RateWindow:
    """Parse Codex primary rate limit window from JSON-RPC result payload."""
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


def sanitize_codex_rpc_error(error_payload: object) -> str:
    """Sanitize a JSON-RPC error payload into a short, token-free English message."""
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

    combined = f"{message} {data_str}".lower()
    if "token_revoked" in combined or "token revoked" in combined:
        return "Session expired (token_revoked); sign in again"
    if "unauthorized" in combined or code in (401, -32401) or "401" in message:
        return "Unauthorized (401); sign in again"
    if "forbidden" in combined or code in (403, -32403) or "403" in message:
        return "Forbidden (403); check permissions"
    if "rate_limit" in combined or "rate limit" in combined or code == 429 or "429" in message:
        return "Rate limit exceeded; retry later"

    clean = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", message)
    clean = re.sub(r"[a-f0-9]{32,}", "[REDACTED]", clean)
    clean = clean.strip()
    if not clean:
        return f"Codex RPC error ({code})" if code is not None else "Codex RPC error"
    if len(clean) > 80:
        clean = clean[:77] + "..."
    return clean


# ---------------------------------------------------------------------------
# Antigravity Parsers
# ---------------------------------------------------------------------------


def parse_antigravity_credentials_blob(
    data: Mapping[str, object] | str,
) -> AntigravityCredentials:
    """Parse Antigravity credentials from a JSON string or mapping."""
    parsed: Mapping[str, object]
    if isinstance(data, str):
        try:
            raw: object = json.loads(data)
        except Exception as exc:
            raise UsageProbeError("Invalid Antigravity credentials JSON") from exc
        if not _is_mapping(raw):
            raise UsageProbeError("Invalid Antigravity credentials format")
        parsed = raw
    elif _is_mapping(data):
        parsed = data
    else:
        raise UsageProbeError("Invalid Antigravity credentials format")

    token_raw = parsed.get("token")
    token_obj: Mapping[str, object] = token_raw if _is_mapping(token_raw) else parsed

    access_token_raw = token_obj.get("access_token")
    access_token = access_token_raw.strip() if isinstance(access_token_raw, str) else ""

    refresh_token_raw = token_obj.get("refresh_token")
    refresh_token = (
        refresh_token_raw.strip()
        if isinstance(refresh_token_raw, str) and refresh_token_raw.strip()
        else None
    )

    if not access_token and not refresh_token:
        raise UsageProbeError("Antigravity credentials missing access_token and refresh_token")

    expiry = parse_timestamp_opt(token_obj.get("expiry"))
    client_id_raw = token_obj.get("client_id")
    client_id = client_id_raw if isinstance(client_id_raw, str) else None

    client_sec_raw = token_obj.get("client_secret")
    client_secret = client_sec_raw if isinstance(client_sec_raw, str) else None

    email = parsed.get("id_token_email")
    if not isinstance(email, str) or not email.strip():
        id_token = parsed.get("id_token")
        email = email_from_jwt(str(id_token)) if id_token is not None else None
    else:
        email = email.strip()

    return AntigravityCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expiry=expiry,
        client_id=client_id.strip() if client_id else None,
        client_secret=client_secret.strip() if client_secret else None,
        email=email,
    )


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


def parse_antigravity_windows(
    payload: Mapping[str, object],
) -> tuple[RateWindow | None, RateWindow | None]:
    """Parse Antigravity rate limit windows from RetrieveUserQuotaSummary response."""
    buckets_list = _select_antigravity_buckets(payload)
    if buckets_list is None:
        return None, None

    session_win: RateWindow | None = None
    weekly_win: RateWindow | None = None

    for bucket in buckets_list:
        if not _is_mapping(bucket):
            continue

        window_id = str(
            bucket.get("bucketId", "")
            or bucket.get("window", "")
            or bucket.get("windowId", "")
            or bucket.get("name", "")
        ).lower()

        is_5h = window_id == "5h" or window_id.endswith(("-5h", "5h"))
        is_weekly = (
            window_id == "weekly" or window_id.endswith(("-weekly", "weekly")) or "7d" in window_id
        )
        if not (is_5h or is_weekly):
            continue

        if bucket.get("disabled") is True:
            used_percent = 100.0
        else:
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

            used_percent = round(max(0.0, min(100.0, (1.0 - rem_ratio) * 100.0)), 4)

        reset_raw = bucket.get("resetTime") or bucket.get("resetsAt") or bucket.get("reset_time")
        reset_dt = parse_timestamp_opt(reset_raw)

        if is_5h and session_win is None:
            session_win = RateWindow(
                used_percent=used_percent, resets_at=reset_dt, window_minutes=300
            )
        elif is_weekly and weekly_win is None:
            weekly_win = RateWindow(
                used_percent=used_percent, resets_at=reset_dt, window_minutes=10080
            )

    return session_win, weekly_win
