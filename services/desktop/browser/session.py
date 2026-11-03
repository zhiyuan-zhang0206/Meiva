"""Log in and inject a gateway session cookie into the shared managed Chrome.

The gateway reverse-proxies agent-served pages under auth (session cookie or
Bearer token), so the managed browser needs its own ``meiva_session`` cookie to
open page URLs. This module obtains an opaque server-side session through the
gateway's normal login endpoint, then injects that returned cookie over CDP.

Injection path: ``Network.setCookie`` on a page target's websocket. Chrome
only exposes the Network domain on page targets (the browser-level target
answers ``-32601``), so we create a throwaway ``about:blank`` tab through the
browser-level websocket, set the cookie on it, and close it again. The cookie
is host-only for the configured gateway URL (no Domain attribute, matching the
login cookie), so it is never sent to any other host.

Runs inside the browser-mcp daemon process, which shares the machine with
Chrome and holds its launch-delivered runner API token (the login credential).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Iterator, Mapping
from http.cookies import SimpleCookie
from typing import NotRequired, TypedDict

import httpx
import websockets
from pydantic import TypeAdapter
from websockets.asyncio.client import ClientConnection

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.host.net.auth import MANAGED_BROWSER_USER_AGENT, cookie_name
from base.log import logger

# meiva:note
# CDP endpoint timeouts: Chrome is local, so these only bound a wedged browser.
_HTTP_TIMEOUT_S = 5.0
_WS_TIMEOUT_S = 10.0


# meiva:note
# What this module and its callers read of the CDP answers. Members they do not read are ignored.
class BrowserVersion(TypedDict):
    webSocketDebuggerUrl: str


class Extension(TypedDict):
    id: str
    name: NotRequired[str]
    path: NotRequired[str]


class ExtensionList(TypedDict):
    extensions: list[Extension]


class TargetInfo(TypedDict):
    targetId: str
    type: str
    url: str


class TargetList(TypedDict):
    targetInfos: list[TargetInfo]


class LoadedExtension(TypedDict):
    id: str


class AttachedTarget(TypedDict):
    sessionId: str


class CreatedTarget(TypedDict):
    targetId: str


class CookieSet(TypedDict):
    success: NotRequired[bool]


class _RemoteValue(TypedDict):
    value: NotRequired[object]


class _ExceptionText(TypedDict):
    description: NotRequired[str]


class _ExceptionDetails(TypedDict):
    text: NotRequired[str]
    exception: NotRequired[_ExceptionText]


class Evaluated(TypedDict):
    result: _RemoteValue
    exceptionDetails: NotRequired[_ExceptionDetails]


BROWSER_VERSION = TypeAdapter(BrowserVersion)
EXTENSIONS = TypeAdapter(ExtensionList)
TARGETS = TypeAdapter(TargetList)
LOADED = TypeAdapter(LoadedExtension)
ATTACHED = TypeAdapter(AttachedTarget)
CREATED = TypeAdapter(CreatedTarget)
COOKIE_SET = TypeAdapter(CookieSet)
EVALUATED = TypeAdapter(Evaluated)
NOTHING = TypeAdapter(dict[str, object])


# meiva:note
# Validate a gateway login HTTP response and return `(cookie_name, value, expires_unix)`.
def parse_login_cookie(
    status_code: int,
    set_cookie: str | None,
    *,
    now: int,
) -> tuple[str, str, int]:
    if status_code != 200:
        raise RuntimeError(f"gateway login failed with HTTP {status_code}")

    parsed = SimpleCookie()
    if set_cookie:
        parsed.load(set_cookie)
    name = cookie_name()
    morsel = parsed.get(name)
    if morsel is None or not morsel.value:
        raise RuntimeError("gateway login response has no session cookie")
    raw_max_age = object_of(morsel.get, "max-age")
    try:
        max_age = int(raw_max_age if isinstance(raw_max_age, str) else "")
    except ValueError as exc:
        raise RuntimeError("gateway session cookie has invalid Max-Age") from exc
    if max_age <= 0:
        raise RuntimeError("gateway session cookie has invalid Max-Age")
    return name, morsel.value, now + max_age


async def gateway_session_login(gateway_url: str, secret: str, *, now: int) -> tuple[str, str, int]:
    """Log in and return ``(cookie_name, value, expires_unix)``.

    The login carries the managed-browser user-agent marker, so the gateway's
    sessions list can label this session row as managed Chrome.
    """
    url = f"{gateway_url.rstrip('/')}/api/auth/login"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        response = await client.post(
            url,
            json={"password": secret},
            headers={"User-Agent": MANAGED_BROWSER_USER_AGENT},
        )
    set_cookie = object_of(response.headers.get, "set-cookie")
    return parse_login_cookie(
        response.status_code,
        set_cookie if isinstance(set_cookie, str) else None,
        now=now,
    )


async def http_json(url: str) -> object:
    """GET ``url`` and return the parsed JSON body."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return object_of(resp.json)


# meiva:note
# The browser-level websocket URL of the Chrome on `cdp_port`.
async def browser_ws_url(cdp_port: int) -> str:
    version = BROWSER_VERSION.validate_python(
        await http_json(f"http://127.0.0.1:{cdp_port}/json/version")
    )
    return version["webSocketDebuggerUrl"]


# meiva:note
# Send one CDP request on an open websocket and return its ``result`` as ``answer`` reads it.
#
# ``session_id`` addresses a flattened target session on the browser-level websocket. Messages that
# are not the reply to this request (CDP events, or a reply on another session's traffic) are
# skipped. Raises RuntimeError on a protocol-level error response.
async def cdp_call[Answer](
    ws: ClientConnection,
    ids: Iterator[int],
    method: str,
    params: Mapping[str, object],
    answer: TypeAdapter[Answer],
    *,
    session_id: str | None = None,
) -> Answer:
    request_id = next(ids)
    request: dict[str, object] = {"id": request_id, "method": method, "params": params}
    if session_id is not None:
        request["sessionId"] = session_id
    await ws.send(json.dumps(request))
    while True:
        resp = object_of(json.loads, await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_S))
        if not is_object_dict(resp):
            raise TypeError("a CDP message is not a JSON object")
        if resp.get("id") == request_id:
            break
    if "error" in resp:
        raise RuntimeError(f"CDP {method} failed: {resp['error']}")
    return answer.validate_python(resp.get("result") or {})


# meiva:note
# Evaluate `expression` in an attached session and return its value (by value). Raises
# RuntimeError with the page's exception description when the evaluation threw.
async def cdp_evaluate(
    ws: ClientConnection,
    ids: Iterator[int],
    session_id: str,
    expression: str,
    *,
    await_promise: bool,
) -> object:
    params: dict[str, object] = {"expression": expression, "returnByValue": True}
    if await_promise:
        params["awaitPromise"] = True
    outcome = await cdp_call(ws, ids, "Runtime.evaluate", params, EVALUATED, session_id=session_id)
    if "exceptionDetails" in outcome:
        details = outcome["exceptionDetails"]
        thrown = details.get("exception", {}).get("description") or details.get("text")
        raise RuntimeError(thrown or "the evaluation threw")
    return outcome["result"].get("value")


async def inject_session_cookie(
    cdp_port: int,
    gateway_url: str,
    secret: str,
    *,
    now: int,
) -> None:
    """Log in to the gateway and set its session cookie in managed Chrome.

    The cookie is host-only for ``gateway_url`` with the same flags the login
    flow sets (HttpOnly, SameSite=Lax, Path=/, expiry from Max-Age).

    Raises RuntimeError when Chrome is unreachable or rejects the cookie.
    """
    name, value, expires = await gateway_session_login(gateway_url, secret, now=now)
    ws_url = await browser_ws_url(cdp_port)

    ids = itertools.count(1)  # request ids are per connection
    async with websockets.connect(ws_url) as browser_ws:
        # meiva:note
        # A page target is required for the Network domain; use a throwaway
        # about:blank tab so we never depend on (or disturb) anyone's tabs.
        created = await cdp_call(
            browser_ws, ids, "Target.createTarget", {"url": "about:blank"}, CREATED
        )
        target_id = created["targetId"]
        try:
            page_ws_url = f"ws://127.0.0.1:{cdp_port}/devtools/page/{target_id}"
            async with websockets.connect(page_ws_url) as page_ws:
                result = await cdp_call(
                    page_ws,
                    ids,
                    "Network.setCookie",
                    {
                        "name": name,
                        "value": value,
                        "url": gateway_url.rstrip("/") + "/",
                        "path": "/",
                        "httpOnly": True,
                        "sameSite": "Lax",
                        "expires": expires,
                    },
                    COOKIE_SET,
                )
        finally:
            try:
                await cdp_call(
                    browser_ws, ids, "Target.closeTarget", {"targetId": target_id}, NOTHING
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "[browser-mcp] could not close the cookie-injection tab {}; "
                    "it stays open in the shared Chrome",
                    target_id,
                )

    if not result.get("success"):
        raise RuntimeError(f"Chrome rejected the gateway session cookie: {result}")
