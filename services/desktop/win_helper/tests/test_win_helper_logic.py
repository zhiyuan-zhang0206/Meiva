from __future__ import annotations

import ctypes
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.types import Message, Receive, Scope, Send

from services.desktop.win_helper.auth import BearerAuthMiddleware
from services.desktop.win_helper.constants import (
    CORNER_FAILSAFE_CODE,
    MEIVA_INPUT_TAG,
    OVERLAY_COLOR_RGB,
)
from services.desktop.win_helper.failsafe_logic import (
    CornerFailsafeState,
    is_bridge_input,
    is_in_corner,
)
from services.desktop.win_helper.input_logic import (
    INPUT,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    button_flags,
    normalize_virtual_coords,
    resolve_vk_pure,
)
from services.desktop.win_helper.lease import DesktopLeaseManager
from services.desktop.win_helper.overlay_render import build_dashed_border_bgra
from services.desktop.win_helper.uia_logic import classify_tree_quality
from services.desktop.win_helper.win32_ffi import object_of


def test_corner_hotzone_and_input_tag() -> None:
    assert is_in_corner(0, 0) is True
    assert is_in_corner(4, 4) is True
    assert is_in_corner(-2, 0) is True
    assert is_in_corner(5, 0) is False
    assert is_in_corner(0, 5) is False
    assert is_in_corner(300, 300) is False

    assert is_bridge_input(MEIVA_INPUT_TAG) is True
    assert is_bridge_input(0) is False
    assert is_bridge_input(0x12345678) is False


def test_corner_failsafe_hook_allows_non_corner_and_tagged_input() -> None:
    st = CornerFailsafeState(threshold_px=4)
    assert st.should_trigger_from_hook(500, 400, extra_info=0, has_active_session=True) is False
    assert (
        st.should_trigger_from_hook(0, 0, extra_info=MEIVA_INPUT_TAG, has_active_session=True)
        is False
    )
    assert st.should_trigger_from_hook(1, 1, extra_info=0, has_active_session=False) is False
    assert st.should_trigger_from_hook(2, 3, extra_info=0, has_active_session=True) is True


def test_corner_failsafe_abort_latch_and_recovery() -> None:
    st = CornerFailsafeState(threshold_px=4)
    st, first = st.with_abort(
        (2, 3), now_iso="2026-11-22T10:00:00Z", previous_holder="meiva-agent-7"
    )
    assert first is True
    assert st.abort_count == 1
    err = st.abort_error()
    assert err["ok"] is False
    assert err["code"] == CORNER_FAILSAFE_CODE
    assert err["previous_holder"] == "meiva-agent-7"

    st, still_blocked = st.cleared_for_new_session((1, 2))
    assert still_blocked is not None
    assert still_blocked["code"] == CORNER_FAILSAFE_CODE
    assert st.aborted is True

    st, cleared_err = st.cleared_for_new_session((200, 200))
    assert cleared_err is None
    assert st.aborted is False


def test_desktop_lease_manager_mutual_exclusion_and_expiry(tmp_path: Path) -> None:
    clock = [datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC)]
    mgr = DesktopLeaseManager(tmp_path / "lease.json", now=lambda: clock[0])

    assert mgr.status() is None
    acq = mgr.acquire(holder="agent-1", purpose="cc4", ttl_seconds=60)
    assert acq["ok"] is True
    assert mgr.check_mutating_allowed("agent-1") is None

    denied = mgr.acquire(holder="agent-2", purpose="qa", ttl_seconds=60)
    assert denied["ok"] is False
    assert denied["code"] == "DESKTOP_LEASE_HELD"
    mut_err = mgr.check_mutating_allowed("agent-2")
    assert mut_err is not None
    assert mut_err["code"] == "DESKTOP_LEASE_HELD"

    renewed = mgr.renew(holder="agent-1", ttl_seconds=120)
    assert renewed["ok"] is True

    rel = mgr.release(holder="agent-1")
    assert rel["ok"] is True
    assert rel["released"] is True
    assert mgr.status() is None

    mgr.acquire(holder="agent-3", ttl_seconds=30)
    clock[0] = clock[0] + timedelta(seconds=31)
    assert mgr.status() is None


def test_dashed_border_bgra_uses_sunset_orange_and_transparent_gaps() -> None:
    w, h = 100, 80
    raw = build_dashed_border_bgra(w, h)
    assert len(raw) == w * h * 4

    def pixel(x: int, y: int) -> tuple[int, int, int, int]:
        off = (y * w + x) * 4
        return raw[off], raw[off + 1], raw[off + 2], raw[off + 3]

    r, g, b = OVERLAY_COLOR_RGB
    assert pixel(0, 0) == (b, g, r, 255)
    assert pixel(10, 2) == (b, g, r, 255)
    assert pixel(20, 0) == (0, 0, 0, 0)
    assert pixel(50, 40) == (0, 0, 0, 0)


def test_input_struct_size_and_pure_helpers() -> None:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(INPUT) == 40
    assert resolve_vk_pure("Enter") == 0x0D
    assert resolve_vk_pure("ctrl") == 0x11
    assert resolve_vk_pure("a") == ord("A")
    assert resolve_vk_pure("F12") == 0x7B
    assert button_flags("left") == (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
    assert button_flags("right") == (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
    with pytest.raises(ValueError, match="Unsupported mouse button"):
        button_flags("extra")
    assert normalize_virtual_coords(0, 0, vx=0, vy=0, vw=1920, vh=1080) == (0, 0)
    assert normalize_virtual_coords(1919, 1079, vx=0, vy=0, vw=1920, vh=1080) == (65535, 65535)


def test_uia_tree_quality_classifier() -> None:
    q_canvas, _ = classify_tree_quality(
        window_width=1200, window_height=800, element_count=3, interactive_count=1
    )
    assert q_canvas == "canvas"
    q_sparse, _ = classify_tree_quality(
        window_width=200, window_height=200, element_count=2, interactive_count=1
    )
    assert q_sparse == "sparse"
    q_good, _ = classify_tree_quality(
        window_width=1200, window_height=800, element_count=25, interactive_count=12
    )
    assert q_good == "good"


async def test_bearer_auth_middleware_protects_mcp_and_exposes_healthz() -> None:
    forwarded: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        forwarded.append(str(scope.get("path")))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    app = BearerAuthMiddleware(
        inner,
        "valid-secret-token-1234567890",
        health_payload=lambda: {"ok": True, "overlay_visible": False, "lease_holder": None},
    )

    async def run_req(path: str, headers: list[tuple[bytes, bytes]]) -> tuple[int, bytes]:
        events: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b""}

        async def send(event: Message) -> None:
            events.append(event)

        await app({"type": "http", "path": path, "headers": headers}, receive, send)
        raw_status = object_of(events[0].get, "status")
        assert isinstance(raw_status, int)
        chunks: list[bytes] = []
        for ev in events:
            if object_of(ev.get, "type") == "http.response.body":
                chunk = object_of(ev.get, "body")
                if isinstance(chunk, bytes):
                    chunks.append(chunk)
        return raw_status, b"".join(chunks)

    status, body = await run_req("/healthz", [])
    assert status == 200
    assert json.loads(body)["ok"] is True

    status, _ = await run_req("/mcp", [])
    assert status == 401
    status, _ = await run_req("/mcp", [(b"authorization", b"Bearer wrong")])
    assert status == 401
    assert forwarded == []

    status, body = await run_req(
        "/mcp", [(b"authorization", b"Bearer valid-secret-token-1234567890")]
    )
    assert status == 200
    assert body == b"ok"
    assert forwarded == ["/mcp"]
