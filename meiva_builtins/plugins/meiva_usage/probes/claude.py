# meiva:note
# Probe for Claude Code usage metrics.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.formatters import format_dual_window_card
from meiva_builtins.plugins.meiva_usage.parsers import (
    parse_claude_oauth_blob,
    parse_claude_windows,
)
from meiva_builtins.plugins.meiva_usage.probes.transport import (
    HttpJsonFetcher,
    HttpJsonRequest,
    HttpStatusError,
)
from meiva_builtins.plugins.meiva_usage.types import (
    CardReading,
    ClaudeOAuthCredentials,
    UsageProbeError,
    resolve_user_path,
)


# meiva:note
# Read Claude credentials from macOS Keychain using generic password query.
# Fixed argv executes macOS security CLI without untrusted command injection.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md
def read_macos_keychain_claude_credentials(
    account: str,
    *,
    runner: Callable[[list[str]], str | None] | None = None,
) -> str | None:
    cmd = [
        "security",
        "find-generic-password",
        "-s",
        "Claude Code-credentials",
        "-a",
        account,
        "-w",
    ]
    if runner is not None:
        return runner(cmd)
    import subprocess

    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        return None
    except (subprocess.SubprocessError, OSError):
        return None


def _read_file_credentials(path: Path) -> ClaudeOAuthCredentials | None:
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return parse_claude_oauth_blob(content)
    except UsageProbeError:
        return None


def _discover_auto_claude_credentials(
    user_home: Path,
    environ: Mapping[str, str],
    platform: str,
    effective_keychain: Callable[[str], str | None] | None,
) -> ClaudeOAuthCredentials:
    candidates: list[ClaudeOAuthCredentials] = []
    if platform == "darwin" and effective_keychain is not None:
        user = environ.get("USER", "").strip()
        for acct in ("unknown", user):
            if not acct:
                continue
            raw = effective_keychain(acct)
            if not raw or not raw.strip():
                continue
            try:
                candidates.append(parse_claude_oauth_blob(raw))
            except UsageProbeError:
                continue

    file_creds = _read_file_credentials(user_home / ".claude" / ".credentials.json")
    if file_creds is not None:
        candidates.append(file_creds)

    if not candidates:
        raise UsageProbeError("No Claude Code credentials found on this machine")

    return max(candidates, key=lambda c: c.expires_at)


def _resolve_claude_credentials(
    collector: CollectorConfig,
    user_home: Path,
    environ: Mapping[str, str],
    platform: str,
    effective_keychain: Callable[[str], str | None] | None,
) -> ClaudeOAuthCredentials:
    if collector.credentials_path is not None:
        path = resolve_user_path(collector.credentials_path, user_home=user_home)
        if not path.is_file():
            raise UsageProbeError(f"Claude credentials file not found: {path}")
        try:
            content = path.read_text("utf-8")
        except Exception as exc:
            raise UsageProbeError(f"Claude credentials file unreadable: {path}") from exc
        return parse_claude_oauth_blob(content)

    if collector.keychain_account is not None:
        if effective_keychain is None:
            raise UsageProbeError(
                f"Keychain credentials for Claude account '{collector.keychain_account}' not found"
            )
        raw = effective_keychain(collector.keychain_account)
        if not raw or not raw.strip():
            raise UsageProbeError(
                f"Keychain credentials for Claude account '{collector.keychain_account}' not found"
            )
        return parse_claude_oauth_blob(raw)

    return _discover_auto_claude_credentials(user_home, environ, platform, effective_keychain)


def probe_claude(
    collector: CollectorConfig,
    *,
    user_home: Path,
    environ: Mapping[str, str],
    now: datetime,
    platform: str,
    http_fetch: HttpJsonFetcher,
    keychain_reader: Callable[[str], str | None] | None = None,
    display_tz: tzinfo = UTC,
) -> CardReading:
    del now
    effective_keychain = (
        keychain_reader
        if keychain_reader is not None
        else (read_macos_keychain_claude_credentials if platform == "darwin" else None)
    )
    creds = _resolve_claude_credentials(collector, user_home, environ, platform, effective_keychain)
    req = HttpJsonRequest(
        url="https://api.anthropic.com/api/oauth/usage",
        method="GET",
        headers={
            "Authorization": f"Bearer {creds.access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )

    try:
        payload = http_fetch(req)
    except HttpStatusError as exc:
        if exc.status_code in (401, 403):
            raise UsageProbeError(
                f"Claude OAuth unauthorized ({exc.status_code}); sign in again"
            ) from None
        raise UsageProbeError(f"Claude usage request failed with HTTP {exc.status_code}") from None

    session, weekly = parse_claude_windows(payload)
    return format_dual_window_card(session, weekly, display_tz=display_tz)
