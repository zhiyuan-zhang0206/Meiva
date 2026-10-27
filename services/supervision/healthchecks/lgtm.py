"""Read-only probes for the designated observability station.

The endpoint probes observe local Loki, Prometheus and Grafana readiness. The
write-path probe sends a unique Loki log and queries it back, with bounded
admission retries. Probe results carry no authority to restart a backend;
service lifecycle belongs to the root supervisor.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import Any, assert_never

from base.clock import Clock
from base.cluster.machine import MachineIdentityReader, MachineRoleMissing, MachineRoles
from base.daemon.health import DaemonProbe
from services.supervision.healthchecks.plans.lgtm_plan import (
    NANOSECONDS_PER_SECOND,
    Accepted,
    Failed,
    Retry,
    marker_visible,
    probe_payload,
    push_step,
    query_params,
    readiness_failure,
)


def _now_ns(clock: Clock) -> int:
    return int(clock.now().timestamp() * NANOSECONDS_PER_SECOND)


def open_local(request: str | urllib.request.Request, *, timeout: float) -> HTTPResponse:
    """Open a loopback URL without consulting the environment's proxies."""
    res: Any = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        request, timeout=timeout
    )
    return res


_WRITE_PROBE_END_LAG_SECONDS = 1
# Two bounded retries for Loki admission throttling; one probe makes at most three pushes.
_WRITE_PROBE_RETRY_BACKOFF_SECONDS = (1, 2)


# meiva:note
# The machine-identity marker that designates the host of `home` as the one running the local
# LGTM backends (`<home>/lgtm-host`, operator-created once).
def lgtm_host_marker(home: Path) -> Path:
    return home / "lgtm-host"


# meiva:note
# The capability set `is_lgtm_host` decides on: the unit's roles, or None while none is configured
# (an unconfigured unit, a bootstrap process). An invalid capability set raises
# `MachineRoleInvalid`, so the answer never degrades to the marker alone.
def lgtm_roles(machine: MachineIdentityReader) -> MachineRoles | None:
    try:
        return machine.role()
    except MachineRoleMissing:
        return None


# meiva:note
# Whether the unit at `home` is the observability station: the `lgtm-host` marker
# OR the declarative `observability-station` capability in its `roles`.
#
# `roles` is the unit's capability set, or None while it has no configured role (`lgtm_roles`):
# then the marker alone decides.
def is_lgtm_host(home: Path, *, roles: MachineRoles | None) -> bool:
    return lgtm_host_marker(home).exists() or (
        roles is not None and "observability-station" in roles
    )


# meiva:note
# Ask the readiness endpoint at `url` of backend `name` and judge the answer.
def readiness_at(name: str, url: str) -> DaemonProbe:
    try:
        with open_local(url, timeout=2.0) as response:
            status: int = response.status
            database = (
                json.loads(response.read())["database"]
                if name == "grafana" and 200 <= status < 300
                else None
            )
    except (OSError, ValueError, KeyError) as exc:
        return DaemonProbe.down(f"{name} readiness failed: {type(exc).__name__}")
    failure = readiness_failure(name=name, status=status, grafana_database=database)
    return (
        DaemonProbe.down(failure)
        if failure is not None
        else DaemonProbe.up(f"{name} readiness accepted")
    )


# meiva:note
# Observe the readiness protocol of backend `name` at its base URL in `urls` (the configured local
# binds, wildcard listeners already resolved to loopback by the caller).
def probe_backend(name: str, *, urls: Mapping[str, str]) -> DaemonProbe:
    from base.telemetry.lgtm_local import HEALTH_PATHS

    if urllib.parse.urlsplit(urls[name]).port is None:
        return DaemonProbe.unavailable("backend has no explicit local port")
    return readiness_at(name, urls[name] + HEALTH_PATHS[name])


# meiva:note
# Send one unique OTLP log to the Loki at `loki_url` and verify that Loki made it queryable. A
# throttled push is retried once per entry of `backoff_s`, pausing that many seconds first on
# `clock`.
def write_path_probe(
    loki_url: str,
    *,
    clock: Clock,
    backoff_s: tuple[float, ...] = _WRITE_PROBE_RETRY_BACKOFF_SECONDS,
) -> tuple[bool, str]:
    now_ns = _now_ns(clock)
    marker_ns = now_ns - (_WRITE_PROBE_END_LAG_SECONDS * NANOSECONDS_PER_SECOND)
    marker, body = probe_payload(marker_ns)
    push_request = urllib.request.Request(  # noqa: S310 — configured Loki endpoint, deliberate
        f"{loki_url}/otlp/v1/logs",
        data=body,
        headers={"Content-Type": "application/json", "X-Scope-OrgID": "fake"},
        method="POST",
    )
    push_failure = _push_probe(push_request, backoff_s, clock=clock)
    if push_failure is not None:
        return False, push_failure

    # The range end is exclusive, so it must sit strictly past the pushed
    # line's timestamp. Capture a fresh now after the push (never the
    # pre-push marker time) and anchor the window on the marker instead.
    query = urllib.parse.urlencode(
        query_params(marker=marker, marker_ns=marker_ns, query_end_ns=_now_ns(clock))
    )
    query_request = urllib.request.Request(  # noqa: S310 — configured Loki endpoint, deliberate
        f"{loki_url}/loki/api/v1/query_range?{query}",
        headers={"X-Scope-OrgID": "fake"},
    )
    try:
        with open_local(query_request, timeout=2.0) as response:
            if not 200 <= response.status < 300:
                return False, "query_error"
            visible = marker_visible(json.loads(response.read()), marker)
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        return False, "query_error"
    return (True, "ok") if visible else (False, "probe_not_visible")


# meiva:note
# Push once, retrying only a throttle, with the bounded pauses of `backoff_s`.
def _push_probe(
    request: urllib.request.Request, backoff_s: tuple[float, ...], *, clock: Clock
) -> str | None:
    attempt = 0
    while True:
        try:
            with open_local(request, timeout=2.0) as response:
                status = response.status
                response_body = response.read() if status >= 500 else b""
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read() if status >= 500 else b""
        except (OSError, HTTPException):
            return "push_error"
        step = push_step(status=status, body=response_body, attempt=attempt, backoff_s=backoff_s)
        match step:
            case Accepted():
                return None
            case Retry(sleep_s=sleep_s):
                clock.sleep(sleep_s)
                attempt += 1
            case Failed(reason=reason):
                return reason
            case _:
                assert_never(step)
