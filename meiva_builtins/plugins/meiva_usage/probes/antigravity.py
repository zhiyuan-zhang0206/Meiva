# meiva:note
# Probe for Antigravity (Gemini CLI / Cloud Code) usage metrics.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from meiva_builtins.plugins.meiva_usage.credentials import parse_antigravity_credentials_blob
from meiva_builtins.plugins.meiva_usage.default_config import (
    CollectorConfig,
    antigravity_card_ids,
)
from meiva_builtins.plugins.meiva_usage.formatters import format_antigravity_group_card
from meiva_builtins.plugins.meiva_usage.parsers import parse_antigravity_quota_groups
from meiva_builtins.plugins.meiva_usage.probes.transport import (
    HttpJsonFetcher,
    HttpJsonRequest,
    HttpStatusError,
)
from meiva_builtins.plugins.meiva_usage.types import (
    AntigravityCredentials,
    CardReading,
    UsageProbeError,
    WindowPair,
    resolve_user_path,
)

_ANTIGRAVITY_UA_VERSION = "2.30.0"


def _epoch_utc() -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC)


def _read_candidate_creds(path: Path, account_email: str | None) -> AntigravityCredentials | None:
    if not path.is_file():
        return None
    try:
        content = path.read_text("utf-8")
        creds = parse_antigravity_credentials_blob(content)
    except (UsageProbeError, OSError):
        return None

    if account_email is not None:
        expected = account_email.strip().lower()
        if not creds.email or creds.email.strip().lower() != expected:
            return None
    return creds


def _missing_credentials_error(
    collector: CollectorConfig, candidate_paths: list[Path]
) -> UsageProbeError:
    if collector.credentials_path is not None:
        return UsageProbeError(f"Antigravity credentials file not found: {candidate_paths[0]}")
    if collector.account_email is not None:
        return UsageProbeError(
            f"No Antigravity credentials found for account '{collector.account_email}'"
        )
    return UsageProbeError("No Antigravity credentials found on this machine")


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
        creds = _read_candidate_creds(path, collector.account_email)
        if creds is not None:
            valid_candidates.append((path, creds))

    if not valid_candidates:
        raise _missing_credentials_error(collector, candidate_paths)

    _chosen_path, chosen_creds = max(
        valid_candidates,
        key=lambda item: (item[1].expiry or _epoch_utc(), item[0] == cli_path),
    )
    return chosen_creds


def _resolve_oauth_param(
    explicit_val: str | None,
    env_var_name: str | None,
    environ: Mapping[str, str],
) -> str | None:
    if explicit_val:
        return explicit_val
    if env_var_name:
        return environ.get(env_var_name, "").strip() or None
    return None


def _refresh_token(
    creds: AntigravityCredentials,
    collector: CollectorConfig,
    environ: Mapping[str, str],
    http_fetch: HttpJsonFetcher,
) -> str:
    refresh_token = creds.refresh_token
    client_id = _resolve_oauth_param(creds.client_id, collector.oauth_client_id_env, environ)
    client_secret = _resolve_oauth_param(
        creds.client_secret, collector.oauth_client_secret_env, environ
    )

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


def _antigravity_user_agent(platform: str) -> str:
    plat = platform.lower()
    if plat.startswith("darwin"):
        os_arch = "darwin/arm64"
    elif plat.startswith("win"):
        os_arch = "windows/amd64"
    else:
        os_arch = "linux/amd64"
    return f"antigravity/{_ANTIGRAVITY_UA_VERSION} {os_arch}"


def _fetch_quota_raw(
    token: str,
    platform: str,
    http_fetch: HttpJsonFetcher,
) -> Mapping[str, object]:
    quota_req = HttpJsonRequest(
        url="https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _antigravity_user_agent(platform),
            "Accept": "application/json",
        },
        body=b"{}",
    )
    return http_fetch(quota_req)


def _fetch_quota_with_auth_retry(
    access_token: str,
    *,
    token_refreshed: bool,
    collector: CollectorConfig,
    chosen_creds: AntigravityCredentials,
    environ: Mapping[str, str],
    platform: str,
    http_fetch: HttpJsonFetcher,
) -> Mapping[str, object]:
    try:
        return _fetch_quota_raw(access_token, platform, http_fetch)
    except HttpStatusError as exc:
        if exc.status_code not in (401, 403):
            raise UsageProbeError(
                f"Antigravity quota request failed with HTTP {exc.status_code}"
            ) from None
        if token_refreshed:
            raise UsageProbeError(
                f"Antigravity quota unauthorized ({exc.status_code}); sign in again"
            ) from None
        new_token = _refresh_token(chosen_creds, collector, environ, http_fetch)
        try:
            return _fetch_quota_raw(new_token, platform, http_fetch)
        except HttpStatusError as retry_exc:
            if retry_exc.status_code in (401, 403):
                raise UsageProbeError(
                    f"Antigravity quota unauthorized ({retry_exc.status_code}); sign in again"
                ) from None
            raise UsageProbeError(
                f"Antigravity quota request failed with HTTP {retry_exc.status_code}"
            ) from None


def _format_gemini_reading(gemini_group: WindowPair | None, display_tz: tzinfo) -> CardReading:
    if gemini_group is not None:
        return format_antigravity_group_card(gemini_group, display_tz=display_tz)
    return CardReading(
        value="—",
        detail="Gemini Models quota group missing from response",
        status="error",
    )


def _format_claude_gpt_reading(
    claude_gpt_group: WindowPair | None, display_tz: tzinfo
) -> CardReading:
    if claude_gpt_group is not None:
        return format_antigravity_group_card(claude_gpt_group, display_tz=display_tz)
    return CardReading(
        value="—",
        detail="Claude and GPT models quota group missing from response",
        status="error",
    )


def probe_antigravity(
    collector: CollectorConfig,
    *,
    user_home: Path,
    environ: Mapping[str, str],
    now: datetime,
    platform: str,
    http_fetch: HttpJsonFetcher,
    display_tz: tzinfo = UTC,
) -> dict[str, CardReading]:
    chosen_creds = _resolve_candidate_credentials(collector, user_home=user_home)

    access_token = chosen_creds.access_token
    token_refreshed = False

    should_refresh_upfront = (not access_token) or (
        chosen_creds.expiry is not None and chosen_creds.expiry <= now + timedelta(seconds=60)
    )

    if should_refresh_upfront:
        access_token = _refresh_token(chosen_creds, collector, environ, http_fetch)
        token_refreshed = True

    payload = _fetch_quota_with_auth_retry(
        access_token,
        token_refreshed=token_refreshed,
        collector=collector,
        chosen_creds=chosen_creds,
        environ=environ,
        platform=platform,
        http_fetch=http_fetch,
    )

    groups = parse_antigravity_quota_groups(payload)
    gemini_id, claude_gpt_id = antigravity_card_ids(collector.id)
    return {
        gemini_id: _format_gemini_reading(groups.gemini, display_tz),
        claude_gpt_id: _format_claude_gpt_reading(groups.claude_gpt, display_tz),
    }
