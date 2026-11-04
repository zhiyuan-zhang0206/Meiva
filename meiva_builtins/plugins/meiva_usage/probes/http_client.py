# meiva:note
# HTTP JSON client implementation using standard library urllib.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from functools import partial
from http.client import HTTPResponse
from typing import TypeGuard

from base.host.boundary.untyped import object_of
from meiva_builtins.plugins.meiva_usage.probes.transport import HttpJsonRequest, HttpStatusError
from meiva_builtins.plugins.meiva_usage.types import UsageProbeError


def _is_mapping(val: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(val, Mapping)


# meiva:note
# The body of the answer `opener(request)` gives. `urlopen` types its answer `Any`; a parameter typed
# with the real `HTTPResponse` takes it without naming the `Any`. The scheme check in the caller
# keeps the answer an `HTTPResponse` for the http/https URLs it admits.
def _read_body(
    opener: Callable[[urllib.request.Request], AbstractContextManager[HTTPResponse]],
    request: urllib.request.Request,
) -> bytes:
    with opener(request) as response:
        return response.read()


# meiva:note
# Execute an HTTP request and parse JSON response without leaking secrets.
# Scheme is validated to http/https; urllib is used for zero third-party dependencies.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md
def urllib_json_fetcher(req: HttpJsonRequest) -> Mapping[str, object]:
    parsed_url = urllib.parse.urlparse(req.url)
    if parsed_url.scheme not in ("http", "https"):
        raise UsageProbeError(f"Unsupported URL scheme: {parsed_url.scheme}")
    host = parsed_url.hostname or req.url

    headers = dict(req.headers)
    request = urllib.request.Request(  # noqa: S310
        url=req.url,
        data=req.body,
        headers=headers,
        method=req.method,
    )

    try:
        content_bytes = _read_body(partial(urllib.request.urlopen, timeout=req.timeout_s), request)
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(exc.code, str(exc.reason)) from None
    except urllib.error.URLError as exc:
        raise UsageProbeError(f"HTTP request to {host} failed: {exc.reason}") from None
    except TimeoutError:
        raise UsageProbeError(f"HTTP request to {host} timed out") from None
    except Exception as exc:
        raise UsageProbeError(f"HTTP request to {host} failed: {exc}") from None

    try:
        decoded = object_of(json.loads, content_bytes.decode("utf-8"))
    except Exception:
        raise UsageProbeError(f"Invalid JSON response from {host}") from None

    if not _is_mapping(decoded):
        raise UsageProbeError(f"Expected JSON object from {host}")
    return decoded
