# meiva:note
# Probe for OpenAI Codex rate limit metrics.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import contextlib
import json
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TypeGuard

from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.formatters import format_codex_window_card
from meiva_builtins.plugins.meiva_usage.parsers import (
    parse_codex_primary_window,
    sanitize_codex_rpc_error,
)
from meiva_builtins.plugins.meiva_usage.types import (
    CardReading,
    UsageProbeError,
    email_from_jwt,
    parse_timestamp_opt,
    resolve_user_path,
)


def _epoch_utc() -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC)


def _is_mapping(val: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(val, Mapping)


CodexRpcRunner = Callable[[Path, Mapping[str, str], float], Mapping[str, object]]


def find_codex_binary(
    environ: Mapping[str, str],
    *,
    user_home: Path | None = None,
) -> str | None:
    path_val = environ.get("PATH", "")
    for segment in path_val.split(":"):
        trimmed = segment.strip()
        if not trimmed:
            continue
        candidate = Path(trimmed) / "codex"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    fallback_candidates: list[Path] = []
    if user_home is not None:
        fallback_candidates.append(user_home / ".local" / "bin" / "codex")
    fallback_candidates.append(Path("/opt/homebrew/bin/codex"))

    for candidate in fallback_candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


def _parse_json_object(line: str) -> Mapping[str, object] | None:
    try:
        raw: object = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return raw if _is_mapping(raw) else None


def _read_json_file(path: Path) -> Mapping[str, object] | None:
    try:
        raw: object = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return raw if _is_mapping(raw) else None


def _read_next_rpc_message(
    proc: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    deadline: float,
    pending: list[bytes],
) -> Mapping[str, object]:
    while True:
        buf = pending[0]
        if b"\n" in buf:
            line_bytes, rest = buf.split(b"\n", 1)
            pending[0] = rest
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            parsed = _parse_json_object(line)
            if parsed is not None:
                return parsed
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UsageProbeError("codex app-server timed out")

        events = selector.select(timeout=max(0.0, remaining))
        if not events:
            raise UsageProbeError("codex app-server timed out")

        if proc.stdout is None:
            raise UsageProbeError("codex app-server stdout closed")
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            raise UsageProbeError("codex app-server exited before response")
        pending[0] = buf + chunk


def _communicate_codex_rpc(
    proc: subprocess.Popen[bytes],
    timeout_s: float = 10.0,
) -> Mapping[str, object]:
    if proc.stdin is None or proc.stdout is None:
        raise UsageProbeError("Failed to open stdio pipes for codex app-server")

    deadline = time.monotonic() + timeout_s
    pending: list[bytes] = [b""]

    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)

        init_req = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "meiva-usage", "version": "1.0"}},
            }
        )
        try:
            proc.stdin.write((init_req + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError as exc:
            raise UsageProbeError(f"Failed to write to codex app-server: {exc}") from exc

        while True:
            msg = _read_next_rpc_message(proc, selector, deadline, pending)
            if msg.get("id") == 1:
                if "error" in msg:
                    clean_msg = sanitize_codex_rpc_error(msg["error"])
                    raise UsageProbeError(clean_msg)
                break

        read_req = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": None,
            }
        )
        try:
            proc.stdin.write((read_req + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError as exc:
            raise UsageProbeError(f"Failed to write to codex app-server: {exc}") from exc

        while True:
            msg = _read_next_rpc_message(proc, selector, deadline, pending)
            if msg.get("id") == 2:
                if "error" in msg:
                    clean_msg = sanitize_codex_rpc_error(msg["error"])
                    raise UsageProbeError(clean_msg)
                if "result" not in msg:
                    raise UsageProbeError("codex app-server returned no result")
                raw_result = msg["result"]
                if not _is_mapping(raw_result):
                    raise UsageProbeError("codex app-server returned non-object result")
                return raw_result


# meiva:note
# Execute initialize and rateLimits read over JSON-RPC with codex app-server.
# Fixed argv runs the local codex app-server child process.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md
def default_codex_rpc_runner(
    home: Path,
    environ: Mapping[str, str],
    timeout_s: float = 10.0,
    *,
    user_home: Path | None = None,
    binary_finder: Callable[..., str | None] | None = None,
) -> Mapping[str, object]:
    finder = binary_finder or find_codex_binary
    effective_home = (
        user_home
        if user_home is not None
        else (home.parent if home.name.startswith(".codex") else None)
    )
    try:
        codex_bin = finder(environ, user_home=effective_home)
    except TypeError:
        codex_bin = finder(environ)
    if not codex_bin:
        raise UsageProbeError("codex binary not found in PATH")

    env = dict(environ)
    env["CODEX_HOME"] = str(home)

    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(  # noqa: S603
            [codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return _communicate_codex_rpc(proc, timeout_s=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsageProbeError(f"Codex RPC runner failed: {exc}") from exc
    finally:
        if proc is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                proc.kill()
                proc.wait(timeout=2.0)


def _collect_matching_homes(
    possible_dirs: list[Path],
    target_email: str | None,
) -> list[tuple[Path, datetime]]:
    homes_with_meta: list[tuple[Path, datetime]] = []
    for dir_path in possible_dirs:
        auth_file = dir_path / "auth.json"
        if not auth_file.is_file():
            continue
        raw_auth = _read_json_file(auth_file)
        if raw_auth is None:
            continue
        tokens_raw = raw_auth.get("tokens")
        tokens = tokens_raw if _is_mapping(tokens_raw) else None

        if target_email is not None:
            id_token = tokens.get("id_token") if tokens is not None else None
            email = email_from_jwt(str(id_token)) if id_token is not None else None
            if not email or email.strip().lower() != target_email:
                continue

        last_refresh_raw = (
            tokens.get("last_refresh") if tokens is not None else None
        ) or raw_auth.get("last_refresh")
        dt = parse_timestamp_opt(last_refresh_raw) or _epoch_utc()
        homes_with_meta.append((dir_path, dt))
    return homes_with_meta


def _resolve_candidate_homes(
    collector: CollectorConfig,
    *,
    user_home: Path,
) -> list[Path]:
    if collector.credentials_path is not None:
        path = resolve_user_path(collector.credentials_path, user_home=user_home)
        if path.is_dir() or path.is_file():
            return [path if path.is_dir() else path.parent]
        raise UsageProbeError(f"Codex home not found: {path}")

    possible_dirs = [user_home / ".codex"]
    if user_home.is_dir():
        for entry in user_home.iterdir():
            if entry.is_dir() and entry.name.startswith(".codex-"):
                possible_dirs.append(entry)

    if collector.account_email is not None:
        target_email = collector.account_email.strip().lower()
        homes = _collect_matching_homes(possible_dirs, target_email)
    else:
        default_home = user_home / ".codex"
        if (default_home / "auth.json").is_file():
            return [default_home]
        homes = _collect_matching_homes(possible_dirs, None)

    homes.sort(
        key=lambda item: (item[1], item[0].name == ".codex"),
        reverse=True,
    )
    return [item[0] for item in homes]


def probe_codex(
    collector: CollectorConfig,
    *,
    user_home: Path,
    environ: Mapping[str, str],
    now: datetime,
    platform: str,
    codex_rpc_runner: CodexRpcRunner | None = None,
    display_tz: tzinfo = UTC,
    timeout_s: float = 10.0,
) -> CardReading:
    del now, platform
    candidate_homes = _resolve_candidate_homes(collector, user_home=user_home)

    if not candidate_homes:
        if collector.credentials_path is not None:
            raise UsageProbeError(f"Codex home not found: {collector.credentials_path}")
        if collector.account_email is not None:
            raise UsageProbeError(f"No Codex home found for account '{collector.account_email}'")
        raise UsageProbeError("No Codex home found on this machine")

    payload: Mapping[str, object] | None = None
    last_error: Exception | None = None

    for home in candidate_homes[:3]:
        try:
            if codex_rpc_runner is None:
                payload = default_codex_rpc_runner(home, environ, timeout_s, user_home=user_home)
            else:
                payload = codex_rpc_runner(home, environ, timeout_s)
            break
        except Exception as exc:
            last_error = exc

    if payload is None:
        if isinstance(last_error, UsageProbeError):
            raise last_error
        raise UsageProbeError(f"Codex probe failed: {last_error}") from last_error

    window = parse_codex_primary_window(payload)
    return format_codex_window_card(window, display_tz=display_tz)
