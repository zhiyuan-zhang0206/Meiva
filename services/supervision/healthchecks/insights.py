from __future__ import annotations

from pathlib import Path
from typing import Any

from base.daemon.health import DaemonProbe
from base.paths import insights_pidfile, insights_socket

_TIMEOUT_S = 3.0


def insights_probe(home: Path) -> DaemonProbe:
    import httpx

    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(insights_socket(home))),
            base_url="http://insights",
            timeout=_TIMEOUT_S,
        ) as client:
            resp = client.get("/healthz")
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        return DaemonProbe.down(
            f"GET /healthz on the insights socket failed ({type(exc).__name__}: {exc})"
        )
    payload_any: Any = payload
    if not isinstance(payload, dict):
        return DaemonProbe.down("the insights socket answered with a non-object")
    typed_payload: dict[str, Any] = payload_any
    if typed_payload.get("name") != "insights":
        return DaemonProbe.down("the insights socket answered as another service")
    if typed_payload.get("home") != str(home):
        return DaemonProbe.down(
            f"the insights socket belongs to home {typed_payload.get('home')!r}"
        )
    try:
        recorded = int(insights_pidfile(home).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return DaemonProbe.down("no readable pid in the insights pidfile")
    if typed_payload.get("pid") != recorded:
        return DaemonProbe.down(
            f"the answering pid {typed_payload.get('pid')} is not the recorded {recorded}"
        )
    return DaemonProbe.up("insights answered /healthz as this home's recorded process")
