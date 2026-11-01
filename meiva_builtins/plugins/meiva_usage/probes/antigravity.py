# meiva:note
# Probe for Antigravity (Gemini CLI / Cloud Code) usage metrics.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from meiva_builtins.plugins.meiva_usage.credentials import parse_antigravity_credentials_blob
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.formatters import format_dual_window_card
from meiva_builtins.plugins.meiva_usage.parsers import parse_antigravity_windows
from meiva_builtins.plugins.meiva_usage.probes.transport import (
    HttpJsonFetcher,
    HttpJsonRequest,
    HttpStatusError,
)
from meiva_builtins.plugins.meiva_usage.types import (
    AntigravityCredentials,
    CardReading,
    UsageProbeError,
    resolve_user_path,
)


def _epoch_utc() -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC)


def _resolve_candidate_credentials(
    collector: CollectorConfig,
    *,
    user_home: Path,
) -> AntigravityCredentials:
    cli_path = user_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    ide_path = user_home / ".gemini" / "jetski-standalone-oauth-token"

    candidate_paths: list[Path] = (
        [resolve_user_path(collector.credentials_path, user_home=user_home)]
        if collector.credentials_path is not None
        else [cli_path, ide_path]
    )

    valid_candidates: list[tuple[Path, AntigravityCredentials]] = []
    for path in candidate_paths:
        if not path.is_file():
            continue
        try:
            content = path.read_text("utf-8")
            creds = parse_antigravity_credentials_blob(content)
        except (UsageProbeError, OSError):
            continue

        if collector.account_email is not None:
            expected = collector.account_email.strip().lower()
            if not creds.email or creds.email.strip().lower() != expected:
                continue

        valid_candidates.append((path, creds))

    if not valid_candidates:
        if collector.credentials_path is not None:
            raise UsageProbeError(f"Antigravity credentials file not found: {candidate_paths[0]}")
        if collector.account_email is not None:
            raise UsageProbeError(
                f"No Antigravity credentials found for account '{collector.account_email}'"
            )
        raise UsageProbeError("No Antigravity credentials found on this machine")

    _chosen_path, chosen_creds = max(
        valid_candidates,
        key=lambda item: (item[1].expiry or _epoch_utc(), item[0] == cli_path),
    )
    return chosen_creds


def _refresh_token(
    creds: AntigravityCredentials,
    collector: CollectorConfig,
    environ: Mapping[str, str],
    http_fetch: HttpJsonFetcher,
) -> str:
    refresh_token = creds.refresh_token
    client_id = creds.client_id
    if not client_id and collector.oauth_client_id_env:
        client_id = environ.get(collector.oauth_client_id_env, "").strip() or None

    client_secret = creds.client_secret
    if not client_secret and collector.oauth_client_secret_env:
        client_secret = environ.get(collector.oauth_client_secret_env, "").strip() or None

    if not refresh_token or not client_id or not client_secret:
        raise UsageProbeError(
            "Antigravity access token expired; sign in again or configure "
            "oauth_client_id_env and oauth_client_secret_env"
        )

    form_data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")

    refresh_req = HttpJsonRequest(
        url="https://oauth2.googleapis.com/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=form_data,
    )

    try:
        resp = http_fetch(refresh_req)
    except HttpStatusError as exc:
        raise UsageProbeError(
            f"Antigravity token refresh failed (HTTP {exc.status_code}); sign in again"
        ) from None

    new_token = resp.get("access_token")
    if not isinstance(new_token, str) or not new_token.strip():
        raise UsageProbeError("Antigravity token refresh returned no access_token")
    return new_token.strip()


def probe_antigravity(
    collector: CollectorConfig,
    *,
    user_home: Path,
    environ: Mapping[str, str],
    now: datetime,
    platform: str,
    http_fetch: HttpJsonFetcher,
    display_tz: tzinfo = UTC,
) -> CardReading:
    del platform
    chosen_creds = _resolve_candidate_credentials(collector, user_home=user_home)

    access_token = chosen_creds.access_token
    token_refreshed = False

    should_refresh_upfront = (not access_token) or (
        chosen_creds.expiry is not None and chosen_creds.expiry <= now + timedelta(seconds=60)
    )

    if should_refresh_upfront:
        access_token = _refresh_token(chosen_creds, collector, environ, http_fetch)
        token_refreshed = True

    def fetch_quota(token: str) -> Mapping[str, object]:
        quota_req = HttpJsonRequest(
            url="https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity-usage-collector/1.0",
                "Accept": "application/json",
            },
            body=b"{}",
        )
        return http_fetch(quota_req)

    try:
        payload = fetch_quota(access_token)
    except HttpStatusError as exc:
        if exc.status_code in (401, 403):
            if not token_refreshed:
                access_token = _refresh_token(chosen_creds, collector, environ, http_fetch)
                try:
                    payload = fetch_quota(access_token)
                except HttpStatusError as retry_exc:
                    if retry_exc.status_code in (401, 403):
                        raise UsageProbeError(
                            f"Antigravity quota unauthorized ({retry_exc.status_code}); sign in again"
                        ) from None
                    raise UsageProbeError(
                        f"Antigravity quota request failed with HTTP {retry_exc.status_code}"
                    ) from None
            else:
                raise UsageProbeError(
                    f"Antigravity quota unauthorized ({exc.status_code}); sign in again"
                ) from None
        else:
            raise UsageProbeError(
                f"Antigravity quota request failed with HTTP {exc.status_code}"
            ) from None

    session, weekly = parse_antigravity_windows(payload)
    return format_dual_window_card(session, weekly, display_tz=display_tz)
