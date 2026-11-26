# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from http.client import HTTPResponse

from base.daemon.health import DaemonProbe
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

_TIMEOUT_S = 3.0
_MAX_BODY_BYTES = 64 * 1024
_IDENTITY_KEYS = ("status", "model", "revision", "device")


def _fetch(url: str, timeout_s: float) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    open_url: Callable[..., HTTPResponse] = opener.open
    with open_url(url, timeout=timeout_s) as response:
        return response.read(_MAX_BODY_BYTES)


def probe_embeddinggemma(
    *, url: str, model: str, revision: str | None, device: str, timeout_s: float = _TIMEOUT_S
) -> DaemonProbe:
    try:
        body = _fetch(url, timeout_s)
    except OSError as exc:
        return DaemonProbe.down(f"GET {url} failed ({type(exc).__name__}: {exc})")
    try:
        answer = object_of(json.loads, body)
    except ValueError:
        return DaemonProbe.down(f"GET {url} answered without a JSON body")
    if not is_object_dict(answer):
        return DaemonProbe.down(f"GET {url} answered without a JSON object")
    expected = {"status": "ok", "model": model, "device": device}
    if revision is not None:
        expected["revision"] = revision
    differing = [
        key for key in _IDENTITY_KEYS if key in expected and answer.get(key) != expected[key]
    ]
    if differing:
        return DaemonProbe.down(
            f"GET {url} answered with another identity ({', '.join(differing)} differ)"
        )
    return DaemonProbe.up(f"EmbeddingGemma serves {model} on {device} at {url}")
