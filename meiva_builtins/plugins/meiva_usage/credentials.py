# meiva:note
# Pure credential parsing functions for meiva_usage.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypeGuard

from meiva_builtins.plugins.meiva_usage.types import (
    AntigravityCredentials,
    ClaudeOAuthCredentials,
    UsageProbeError,
    email_from_jwt,
    parse_timestamp_opt,
)


def _is_mapping(val: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(val, Mapping)


def parse_claude_oauth_blob(data: Mapping[str, object] | str) -> ClaudeOAuthCredentials:
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
    parsed_dt = parse_timestamp_opt(expires_raw)
    dt = parsed_dt if parsed_dt is not None else datetime.fromtimestamp(0, tz=UTC)

    return ClaudeOAuthCredentials(access_token=token.strip(), expires_at=dt)


def _load_credentials_mapping(data: Mapping[str, object] | str, name: str) -> Mapping[str, object]:
    if isinstance(data, str):
        try:
            raw: object = json.loads(data)
        except Exception as exc:
            raise UsageProbeError(f"Invalid {name} credentials JSON") from exc
        if not _is_mapping(raw):
            raise UsageProbeError(f"Invalid {name} credentials format")
        return raw
    if _is_mapping(data):
        return data
    raise UsageProbeError(f"Invalid {name} credentials format")


def _extract_antigravity_email(parsed: Mapping[str, object]) -> str | None:
    email = parsed.get("id_token_email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    id_token = parsed.get("id_token")
    return email_from_jwt(str(id_token)) if id_token is not None else None


def parse_antigravity_credentials_blob(
    data: Mapping[str, object] | str,
) -> AntigravityCredentials:
    parsed = _load_credentials_mapping(data, "Antigravity")

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
    client_id = (
        client_id_raw.strip() if isinstance(client_id_raw, str) and client_id_raw.strip() else None
    )

    client_sec_raw = token_obj.get("client_secret")
    client_secret = (
        client_sec_raw.strip()
        if isinstance(client_sec_raw, str) and client_sec_raw.strip()
        else None
    )

    return AntigravityCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expiry=expiry,
        client_id=client_id,
        client_secret=client_secret,
        email=_extract_antigravity_email(parsed),
    )
