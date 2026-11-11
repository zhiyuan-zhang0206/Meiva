# meiva:note
# The read-only gateway verbs' shared transport and table rendering.
#
# A read verb is a thin client over one gateway GET: it forwards the filters the operator
# named, prints the answer, and leaves windows and limits to the gateway's own defaults. A
# refusal (an unknown filter value, a 404) prints the gateway's reason on stderr and exits 1,
# so an agent reading the output sees the cause instead of a traceback.

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Mapping, Sequence

import httpx

from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.host.net import http_dial
from cli.boundary.json_body import response_object, response_value

__all__ = ["fetch", "print_json", "render_table"]

_TIMEOUT_S = 30.0


# meiva:note
# The gateway's `detail` string, falling back to the raw body.
def _detail(resp: httpx.Response) -> str:
    with contextlib.suppress(ValueError, TypeError):
        payload = response_object(resp)
        if "detail" in payload:
            return str(payload["detail"])
    return resp.text


# meiva:note
# GET one gateway path with the filters the caller set (a `None` value is not sent). Returns the
# response, or None after printing the gateway's reason on stderr when it refused.
def fetch(
    access: GatewayAccess, path: str, params: Mapping[str, str | int | float | bool | None]
) -> httpx.Response | None:
    sent = {key: value for key, value in params.items() if value is not None}
    resp = http_dial.get(
        f"{gateway_api_base(access.gateway_url)}{path}",
        params=sent,
        headers=gateway_auth_headers(access),
        timeout=_TIMEOUT_S,
    )
    if resp.status_code >= 400:
        print(f"gateway {resp.status_code}: {_detail(resp)}", file=sys.stderr)
        return None
    return resp


# meiva:note
# Print the response body as one compact JSON document, unchanged.
def print_json(resp: httpx.Response) -> None:
    print(json.dumps(response_value(resp), ensure_ascii=False))


# meiva:note
# Left-aligned columns under a header line; each column is as wide as its widest cell. The last
# column is not padded, so a free-text tail adds no trailing spaces.
def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    lines = [headers, *rows]
    return "\n".join(
        "  ".join(
            cell.ljust(widths[i]) if i < len(widths) - 1 else cell for i, cell in enumerate(line)
        )
        for line in lines
    )
