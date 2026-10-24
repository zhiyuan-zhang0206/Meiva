# meiva:note
# The one place a provider request leaves the process: it waits its turn at the rate limiter,
# sends the request through the shared retry policy, and turns an HTTP, connection or JSON
# failure into the caller's error type. The request and the wording come from `wire.py`.
# meiva:link meiva/web/docs/providers.meiva.okf.md

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from base.host.net.resilience import retry
from meiva.web.errors import WebError
from meiva.web.failures import connection_failed_message, http_failed_message, non_json_message
from meiva.web.pacing import reserve
from meiva.web.policy import WEB_READ_POLICY
from meiva.web.wire import HttpRequest

__all__ = ["RateLimiter", "send_json"]

_log = logging.getLogger(__name__)

_DETAIL_MAX = 200


# meiva:note
# Spaces request starts `1/qps` seconds apart across the threads of one batch; a qps of zero or
# below does not pace.
class RateLimiter:
    def __init__(self, qps: float) -> None:
        self._interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if not self._interval:
            return
        with self._lock:
            slot = reserve(self._next_at, time.monotonic(), self._interval)
            self._next_at = slot.next_at
        if slot.wait > 0:
            time.sleep(slot.wait)


def _url(request: HttpRequest) -> str:
    url = request.url + urllib.parse.quote(request.suffix, safe=":/?#&=.@+_")
    return f"{url}?{urllib.parse.urlencode(request.query)}" if request.query else url


# meiva:note
# The endpoint of a request comes from configuration, which is why the scheme is not checked
# here: each provider's default is an https literal and an operator may point it at a relay.
# The page URL of a reader request is quoted so spaces and non-ASCII query text survive; the
# safe set keeps common URL characters to avoid double encoding.
def _read(request: HttpRequest, timeout_s: float) -> bytes:
    prepared = urllib.request.Request(  # noqa: S310
        _url(request), data=request.data, headers=dict(request.headers), method=request.method
    )
    with urllib.request.urlopen(prepared, timeout=timeout_s) as response:  # noqa: S310
        return response.read()


# meiva:note
# Send `request` and decode the JSON answer. A transient failure is retried by the shared web
# read policy; an HTTP status, a refused connection or a body that is not JSON raises
# `error_type` with the failure worded by `wire`. `provider_json` selects the message of a JSON
# error body over the whole body; a `pacer` is awaited before every attempt.
def send_json(
    request: HttpRequest,
    *,
    timeout_s: float,
    label: str,
    error_type: type[WebError],
    provider_json: bool,
    pacer: RateLimiter | None,
) -> dict[str, Any]:
    def attempt() -> bytes:
        if pacer is not None:
            pacer.acquire()
        return _read(request, timeout_s)

    try:
        data: dict[str, Any] = json.loads(retry(WEB_READ_POLICY)(attempt))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        _log.warning(
            "%s HTTP %s after %d attempts: %s",
            label,
            e.code,
            WEB_READ_POLICY.max_attempts,
            detail[:_DETAIL_MAX],
        )
        raise error_type(
            http_failed_message(label, e.code, detail, provider_json=provider_json)
        ) from e
    except urllib.error.URLError as e:
        _log.warning(
            "%s connection failed after %d attempts: %s",
            label,
            WEB_READ_POLICY.max_attempts,
            e.reason,
        )
        raise error_type(connection_failed_message(label, e.reason)) from e
    except json.JSONDecodeError as e:
        raise error_type(non_json_message(label, e)) from e
    return data
