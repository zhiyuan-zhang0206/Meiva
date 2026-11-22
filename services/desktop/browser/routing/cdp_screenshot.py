# meiva:link services/desktop/browser/docs/operations.md

from __future__ import annotations

import asyncio
import itertools
import math
import re
from collections.abc import Mapping

import websockets
from mcp.types import CallToolResult, ImageContent
from pydantic import TypeAdapter

from base.host.boundary.json_values import is_object_dict
from services.desktop.browser.session import browser_ws_url, cdp_call

from .remote_files import MAX_FILE_BYTES, decode_bytes

_RESPONSE = TypeAdapter(dict[str, object])
_TARGET = re.compile(r"[0-9A-Fa-f]{32}")
_FORMATS = frozenset({"png", "jpeg", "webp"})


def _clip(view: object, dpr: object) -> dict[str, float]:
    if not is_object_dict(view) or not isinstance(dpr, int | float) or not math.isfinite(dpr):
        raise ValueError("Chrome did not provide safe screenshot dimensions")
    x, y, width, height, zoom = (
        view.get(key) for key in ("pageX", "pageY", "clientWidth", "clientHeight", "scale")
    )
    values = (x, y, width, height, zoom)
    if not all(isinstance(value, int | float) and math.isfinite(value) for value in values):
        raise ValueError("Chrome did not provide safe screenshot dimensions")
    x, y, width, height, zoom = TypeAdapter(
        tuple[float, float, float, float, float]
    ).validate_python(values)
    if min(width, height, zoom, dpr) <= 0:
        raise ValueError("Chrome did not provide safe screenshot dimensions")
    return {
        "x": x,
        "y": y,
        "width": min(width, 800),
        "height": min(height, 600),
        "scale": min(1, 1 / (dpr * zoom)),
    }


async def capture_owned_viewport(
    port: int,
    target: str,
    args: Mapping[str, object],
    *,
    tab_target: bool = False,
) -> CallToolResult:
    if not _TARGET.fullmatch(target):
        raise ValueError("Invalid owned Chrome target")
    fmt = args.get("format", "png")
    if not isinstance(fmt, str) or fmt not in _FORMATS:
        raise ValueError("Unsupported remote Chrome screenshot format")
    async with (
        asyncio.timeout(8.0),
        websockets.connect(
            await browser_ws_url(port),
            max_size=MAX_FILE_BYTES,
        ) as ws,
    ):
        ids = itertools.count(1)
        attach = await cdp_call(
            ws,
            ids,
            "Target.attachToTarget",
            {"targetId": target, "flatten": True},
            _RESPONSE,
        )
        session_id = attach.get("sessionId")
        if not isinstance(session_id, str):
            raise TypeError("Could not attach to the owned Chrome target")
        if tab_target:
            session_id = await _page_session(ws, ids, session_id)
        dpr = await _device_scale(ws, ids, session_id)
        metrics = await cdp_call(
            ws, ids, "Page.getLayoutMetrics", {}, _RESPONSE, session_id=session_id
        )
        params: dict[str, object] = {
            "format": fmt,
            "captureBeyondViewport": False,
            "clip": _clip(metrics.get("cssVisualViewport"), dpr),
        }
        if fmt != "png":
            params["quality"] = args.get("quality", 80)
        result = await cdp_call(
            ws, ids, "Page.captureScreenshot", params, _RESPONSE, session_id=session_id
        )
    data = result.get("data")
    if not isinstance(data, str):
        raise TypeError("Chrome did not return screenshot data")
    _ = decode_bytes(data)
    return CallToolResult(content=[ImageContent(type="image", mime_type=f"image/{fmt}", data=data)])


async def _device_scale(
    ws: websockets.ClientConnection,
    ids: itertools.count[int],
    session: str,
) -> object:
    tree = await cdp_call(ws, ids, "Page.getFrameTree", {}, _RESPONSE, session_id=session)
    top = tree.get("frameTree")
    frame = top.get("frame") if is_object_dict(top) else None
    frame_id = frame.get("id") if is_object_dict(frame) else None
    if not isinstance(frame_id, str):
        raise TypeError("Chrome did not identify the screenshot frame")
    world = await cdp_call(
        ws,
        ids,
        "Page.createIsolatedWorld",
        {"frameId": frame_id},
        _RESPONSE,
        session_id=session,
    )
    context_id = world.get("executionContextId")
    if type(context_id) is not int:
        raise TypeError("Chrome did not isolate screenshot measurements")
    measured = await cdp_call(
        ws,
        ids,
        "Runtime.evaluate",
        {
            "expression": "globalThis.devicePixelRatio",
            "contextId": context_id,
            "returnByValue": True,
        },
        _RESPONSE,
        session_id=session,
    )
    remote = measured.get("result")
    return remote.get("value") if is_object_dict(remote) else None


async def _page_session(
    ws: websockets.ClientConnection,
    ids: itertools.count[int],
    tab_session: str,
) -> str:
    attached: list[str] = []

    def observe(message: dict[str, object]) -> None:
        if (
            message.get("sessionId") != tab_session
            or message.get("method") != "Target.attachedToTarget"
        ):
            return
        params = message.get("params")
        if not is_object_dict(params) or not isinstance(session := params.get("sessionId"), str):
            return
        target = params.get("targetInfo")
        if is_object_dict(target) and target.get("type") == "page":
            attached.append(session)

    await cdp_call(
        ws,
        ids,
        "Target.setAutoAttach",
        {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
            "filter": [{"type": "page"}, {"exclude": True}],
        },
        _RESPONSE,
        session_id=tab_session,
        observe=observe,
    )
    if len(attached) != 1:
        raise ValueError("Could not identify the active page of the owned Chrome tab")
    return attached[0]
