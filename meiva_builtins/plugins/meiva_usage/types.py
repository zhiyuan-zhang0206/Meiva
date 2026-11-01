# meiva:note
# Domain types, error classes, and pure path helpers for meiva_usage.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeGuard

ERROR_STALE_AFTER_S: float = 30.0 * 60.0


def _is_dict_mapping(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


class UsageProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RateWindow:
    used_percent: float
    resets_at: datetime | None = None
    window_minutes: int | None = None


@dataclass(frozen=True)
class CardReading:
    value: str
    detail: str
    status: Literal["ok", "warn", "error"]


@dataclass(frozen=True)
class ClaudeOAuthCredentials:
    access_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AntigravityCredentials:
    access_token: str
    refresh_token: str | None = None
    expiry: datetime | None = None
    client_id: str | None = None
    client_secret: str | None = None
    email: str | None = None


def should_write_failure(
    existing_status: str | None,
    existing_updated_at: datetime | None,
    now: datetime,
    *,
    max_age_s: float = ERROR_STALE_AFTER_S,
) -> bool:
    if existing_status is None or existing_status == "error":
        return True
    if existing_updated_at is None:
        return True
    age_s = (now - existing_updated_at).total_seconds()
    return age_s > max_age_s


def resolve_user_path(raw: str, *, user_home: Path) -> Path:
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("Path string must not be empty")
    if cleaned == "~":
        return user_home
    if cleaned.startswith("~/"):
        return user_home / cleaned[2:]
    return Path(cleaned)


def parse_timestamp(raw: object) -> datetime:
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e11:
            return datetime.fromtimestamp(val / 1000.0, tz=UTC)
        return datetime.fromtimestamp(val, tz=UTC)
    if isinstance(raw, str):
        cleaned = raw.strip()
        try:
            val = float(cleaned)
        except ValueError:
            val = None
        if val is not None:
            if val > 1e11:
                return datetime.fromtimestamp(val / 1000.0, tz=UTC)
            return datetime.fromtimestamp(val, tz=UTC)
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    raise UsageProbeError(f"Unsupported timestamp value: {raw!r}")


def parse_timestamp_opt(raw: object) -> datetime | None:
    if raw is None:
        return None
    try:
        return parse_timestamp(raw)
    except (UsageProbeError, ValueError, TypeError, OverflowError):
        return None


def email_from_jwt(token: str) -> str | None:
    if not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    try:
        padding = "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64 + padding)
        raw: object = json.loads(decoded.decode("utf-8"))
        if _is_dict_mapping(raw):
            email_val = raw.get("email")
            if isinstance(email_val, str) and email_val.strip():
                return email_val.strip()
    except (ValueError, TypeError):
        return None
    return None
