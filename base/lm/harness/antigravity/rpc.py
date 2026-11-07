from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from http.client import HTTPResponse
from pathlib import Path

import psutil

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

__all__ = [
    "LIVE_SUBMIT_TIMEOUT_SECONDS",
    "AntigravityServer",
    "antigravity_rpc",
    "antigravity_server_from_environment",
    "discover_antigravity_server",
]

_ANTIGRAVITY_SERVICE_PREFIX = "/exa.language_server_pb.LanguageServerService/"
_ANTIGRAVITY_PROBE_TIMEOUT_SECONDS = 0.5
LIVE_SUBMIT_TIMEOUT_SECONDS = 5.0


# meiva:note
# Local Antigravity language_server Connect endpoint and CSRF token.
@dataclass(frozen=True)
class AntigravityServer:
    address: str
    csrf_token: str


def _extract_csrf_token(cmdline: list[str]) -> str | None:
    for idx, arg in enumerate(cmdline):
        if arg == "--csrf_token" and idx + 1 < len(cmdline) and cmdline[idx + 1].strip():
            return cmdline[idx + 1].strip()
        if arg.startswith("--csrf_token="):
            value = arg.removeprefix("--csrf_token=").strip()
            if value:
                return value
    return None


def _is_antigravity_language_server(name: str, cmdline: list[str]) -> bool:
    if name.lower().startswith("language_server"):
        return True
    if cmdline:
        exe_name = Path(cmdline[0]).name.lower()
        return exe_name.startswith("language_server")
    return False


# meiva:note
# Candidate Antigravity servers inspected from listening processes.
def _process_candidate_servers(proc: psutil.Process) -> list[AntigravityServer]:
    try:
        info: dict[str, object] = getattr(proc, "info", {})
        raw_cmd = info.get("cmdline")
        cmdline: list[str] = [str(arg) for arg in raw_cmd] if is_object_list(raw_cmd) else []
        name = str(info.get("name") or "")
        if not _is_antigravity_language_server(name, cmdline):
            return []
        csrf_token = _extract_csrf_token(cmdline)
        if not csrf_token:
            return []
        ports = sorted(
            {
                int(conn.laddr.port)
                for conn in proc.net_connections(kind="inet")
                if conn.status == psutil.CONN_LISTEN and conn.laddr
            }
        )
    except (psutil.Error, OSError, ValueError):
        return []
    return [AntigravityServer(address=f"127.0.0.1:{port}", csrf_token=csrf_token) for port in ports]


def _probe_antigravity_server(candidate: AntigravityServer, timeout: float) -> bool:
    try:
        _ = antigravity_rpc("GetWorkspaceInfos", {}, server=candidate, timeout=timeout)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def antigravity_server_from_environment(environment: Mapping[str, str]) -> AntigravityServer | None:
    address = (environment.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    token = (environment.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()
    return AntigravityServer(address=address, csrf_token=token) if address and token else None


def discover_antigravity_server(
    *, timeout: float = LIVE_SUBMIT_TIMEOUT_SECONDS
) -> AntigravityServer:
    probe_timeout = min(timeout, _ANTIGRAVITY_PROBE_TIMEOUT_SECONDS)
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        for candidate in _process_candidate_servers(proc):
            if _probe_antigravity_server(candidate, probe_timeout):
                return candidate
    raise RuntimeError(
        "No running Antigravity language_server with an HTTP Connect endpoint was found on this machine"
    )


def _read_body(
    opener: Callable[[urllib.request.Request], AbstractContextManager[HTTPResponse]],
    request: urllib.request.Request,
) -> bytes:
    with opener(request) as response:
        return response.read()


def _as_mapping(value: object) -> dict[str, object] | None:
    return value if is_object_dict(value) else None


# meiva:note
# Call one Connect JSON-RPC method on the local Antigravity language_server.
def antigravity_rpc(
    method: str,
    payload: Mapping[str, object],
    *,
    server: AntigravityServer | None = None,
    timeout: float = LIVE_SUBMIT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    target = server or discover_antigravity_server(timeout=timeout)
    url = f"http://{target.address}{_ANTIGRAVITY_SERVICE_PREFIX}{method}"
    body = json.dumps(dict(payload)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-codeium-csrf-token": target.csrf_token,
        },
        method="POST",
    )
    try:
        raw = _read_body(partial(urllib.request.urlopen, timeout=timeout), req).decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(OSError, ValueError):
            detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Antigravity {method} failed (HTTP {exc.code}): {detail or exc.reason}"
        ) from exc
    parsed: object = object_of(json.loads, raw) if raw.strip() else {}
    mapping = _as_mapping(parsed)
    if mapping is None:
        raise RuntimeError(f"Antigravity {method} returned non-object JSON")
    return mapping
