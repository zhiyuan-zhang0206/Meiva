# meiva:note
# HTTP JSON client implementation using standard library urllib.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import TypeGuard

from meiva_builtins.plugins.meiva_usage.probes.transport import HttpJsonRequest, HttpStatusError
from meiva_builtins.plugins.meiva_usage.types import UsageProbeError


def _is_mapping(val: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(val, Mapping)


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
        with urllib.request.urlopen(request, timeout=req.timeout_s) as response:  # noqa: S310
            content_bytes = response.read()
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(exc.code, str(exc.reason)) from None
    except urllib.error.URLError as exc:
        raise UsageProbeError(f"HTTP request to {host} failed: {exc.reason}") from None
    except TimeoutError:
        raise UsageProbeError(f"HTTP request to {host} timed out") from None
    except Exception as exc:
        raise UsageProbeError(f"HTTP request to {host} failed: {exc}") from None

    try:
        decoded: object = json.loads(content_bytes.decode("utf-8"))
    except Exception:
        raise UsageProbeError(f"Invalid JSON response from {host}") from None

    if not _is_mapping(decoded):
        raise UsageProbeError(f"Expected JSON object from {host}")
    return decoded
