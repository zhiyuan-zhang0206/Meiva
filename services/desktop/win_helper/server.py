from __future__ import annotations

import base64
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil
import uvicorn
from mcp import types
from mcp.server.mcpserver import MCPServer

from .auth import BearerAuthMiddleware
from .constants import CORNER_FAILSAFE_PX, DEFAULT_HOST, DEFAULT_PORT
from .lease import DesktopLeaseManager
from .win32_ffi import as_float
from .win32_input import (
    clipboard_get,
    clipboard_set,
    mouse_click,
    mouse_move_drag,
    mouse_scroll,
    press_key,
    type_text,
)
from .win32_overlay import BorderOverlay, CornerFailsafeGuard, get_session_info, init_dpi_awareness
from .win32_uia import (
    UiaWorker,
    capture_screenshot,
    focus_window,
    get_screen_geometry,
    list_windows,
)


def _j(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ram_gb() -> dict[str, float]:
    vals: tuple[object, ...] = tuple(psutil.virtual_memory())
    total = as_float(vals[0]) if len(vals) > 0 else 0.0
    avail = as_float(vals[1]) if len(vals) > 1 else 0.0
    return {"total": round(total / 1e9, 1), "available": round(avail / 1e9, 1)}


def _setup_logging(logs_dir: Path) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("meiva_win_helper")
    logger.setLevel(logging.INFO)
    fh = RotatingFileHandler(
        logs_dir / "bridge.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(fh)
    return logger


def _load_bearer_token(token_file: Path) -> str:
    if not token_file.is_file():
        raise RuntimeError(f"Bearer token file missing: {token_file}")
    token = token_file.read_text(encoding="utf-8").strip()
    if len(token) < 24:
        raise RuntimeError(f"Bearer token in {token_file} is unexpectedly short")
    return token


@dataclass(frozen=True)
class _RuntimeHandles:
    pid: int
    dpi_mode: str
    shots_dir: Path
    now: Callable[[], datetime]
    leases: DesktopLeaseManager
    overlay: BorderOverlay
    guard: CornerFailsafeGuard
    uia: UiaWorker

    def check_mutation_gates(self, holder: str = "") -> str | None:
        failsafe_err = self.guard.check_allowed()
        if failsafe_err is not None:
            return _j(failsafe_err)
        lease_err = self.leases.check_mutating_allowed(holder=holder)
        if lease_err is not None:
            return _j(lease_err)
        return None


def _register_desktop_tools(mcp: MCPServer, rt: _RuntimeHandles) -> None:
    @mcp.tool(name="status")
    def status_tool() -> str:
        wins = list_windows(visible_only=True, exclude_hwnd=rt.overlay.hwnd)
        fg = wins[0] if wins and wins[0].get("is_foreground") else None
        active = rt.leases.status()
        rt.overlay.sync_visibility(visible=active is not None)
        return _j(
            {
                "ok": True,
                "session": get_session_info(pid=rt.pid),
                "dpi_mode": rt.dpi_mode,
                "screen": get_screen_geometry(),
                "ram_gb": _ram_gb(),
                "foreground_window": fg,
                "visible_window_count": len(wins),
                "failsafe": rt.guard.status(),
                "overlay": rt.overlay.status(),
                "lease": active,
            }
        )

    @mcp.tool(name="list_windows")
    def list_windows_tool(*, visible_only: bool = True, title_contains: str = "") -> str:
        wins = list_windows(
            visible_only=visible_only, title_contains=title_contains, exclude_hwnd=rt.overlay.hwnd
        )
        return _j({"ok": True, "count": len(wins), "windows": wins, "failsafe": rt.guard.status()})

    @mcp.tool(name="focus_window")
    def focus_window_tool(title_contains: str = "", hwnd: int = 0, holder: str = "") -> str:
        blocked = rt.check_mutation_gates(holder)
        if blocked:
            return blocked
        return _j(focus_window(hwnd=hwnd, title_contains=title_contains))

    @mcp.tool(name="uia_tree")
    def uia_tree_tool(
        hwnd: int = 0,
        title_contains: str = "",
        mode: str = "interactive",
        max_depth: int = 8,
        max_nodes: int = 150,
        timeout_sec: float = 6.0,
    ) -> str:
        res = rt.uia.uia_tree(
            hwnd=hwnd,
            title_contains=title_contains,
            mode=mode,
            max_depth=max_depth,
            max_nodes=max_nodes,
            timeout_sec=timeout_sec,
        )
        res["session_aborted"] = rt.guard.status()["session_aborted"]
        return _j(res)

    @mcp.tool(name="uia_act")
    def uia_act_tool(
        element_id: str, action: str = "invoke", value: str = "", holder: str = ""
    ) -> str:
        blocked = rt.check_mutation_gates(holder)
        if blocked:
            return blocked
        return _j(rt.uia.uia_act(element_id=element_id, action=action, value=value))

    @mcp.tool(name="screenshot")
    def screenshot_tool(
        x: int = 0,
        y: int = 0,
        width: int = 0,
        height: int = 0,
        hwnd: int = 0,
        title_contains: str = "",
        monitor: int = 1,
        format: str = "webp",
        quality: int = 75,
        max_width: int = 1440,
        save_path: str = "",
    ) -> list[types.TextContent | types.ImageContent]:
        with rt.overlay.hide_for_capture():
            meta, img_bytes, mime = capture_screenshot(
                shots_dir=rt.shots_dir,
                now=rt.now,
                x=x,
                y=y,
                width=width,
                height=height,
                hwnd=hwnd,
                title_contains=title_contains,
                monitor=monitor,
                fmt=format,
                quality=quality,
                max_width=max_width,
                save_path=save_path,
            )
        meta["session_aborted"] = rt.guard.status()["session_aborted"]
        meta["overlay_hidden_during_capture"] = True
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return [
            types.TextContent(type="text", text=_j(meta)),
            types.ImageContent(type="image", data=b64, mime_type=mime),
        ]

    del (
        status_tool,
        list_windows_tool,
        focus_window_tool,
        uia_tree_tool,
        uia_act_tool,
        screenshot_tool,
    )


def _register_input_tools(mcp: MCPServer, rt: _RuntimeHandles) -> None:
    @mcp.tool(name="mouse_click")
    def mouse_click_tool(
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
        modifiers: str = "",
        holder: str = "",
    ) -> str:
        blocked = rt.check_mutation_gates(holder)
        if blocked:
            return blocked
        return _j(mouse_click(x, y, button=button, clicks=clicks, modifiers=modifiers))

    @mcp.tool(name="mouse_move_drag")
    def mouse_move_drag_tool(
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str = "left",
        duration_ms: int = 250,
        steps: int = 12,
        holder: str = "",
    ) -> str:
        blocked = rt.check_mutation_gates(holder)
        if blocked:
            return blocked
        return _j(
            mouse_move_drag(
                start_x, start_y, end_x, end_y, button=button, duration_ms=duration_ms, steps=steps
            )
        )

    @mcp.tool(name="mouse_scroll")
    def mouse_scroll_tool(x: int, y: int, dy: int, dx: int = 0, holder: str = "") -> str:
        blocked = rt.check_mutation_gates(holder)
        if blocked:
            return blocked
        return _j(mouse_scroll(x, y, dy, dx=dx))

    @mcp.tool(name="type_text")
    def type_text_tool(
        text: str, *, enter: bool = False, clear_first: bool = False, holder: str = ""
    ) -> str:
        blocked = rt.check_mutation_gates(holder)
        if blocked:
            return blocked
        return _j(type_text(text, enter=enter, clear_first=clear_first))

    @mcp.tool(name="press_key")
    def press_key_tool(keys: str, holder: str = "") -> str:
        blocked = rt.check_mutation_gates(holder)
        if blocked:
            return blocked
        return _j(press_key(keys))

    @mcp.tool(name="clipboard_get")
    def clipboard_get_tool() -> str:
        return _j(clipboard_get())

    @mcp.tool(name="clipboard_set")
    def clipboard_set_tool(text: str, holder: str = "") -> str:
        blocked = rt.check_mutation_gates(holder)
        if blocked:
            return blocked
        return _j(clipboard_set(text))

    del (
        mouse_click_tool,
        mouse_move_drag_tool,
        mouse_scroll_tool,
        type_text_tool,
        press_key_tool,
        clipboard_get_tool,
        clipboard_set_tool,
    )


def _register_control_tools(mcp: MCPServer, rt: _RuntimeHandles) -> None:
    @mcp.tool(name="lease_manage")
    def lease_manage_tool(
        action: str = "status",
        holder: str = "",
        purpose: str = "",
        ttl_seconds: int = 1800,
        *,
        force: bool = False,
    ) -> str:
        act = action.strip().lower()
        if act == "status":
            active = rt.leases.status()
            rt.overlay.sync_visibility(visible=active is not None)
            return _j(
                {
                    "ok": True,
                    "lease": active,
                    "overlay": rt.overlay.status(),
                    "failsafe": rt.guard.status(),
                }
            )
        if act == "acquire":
            err = rt.guard.clear_for_new_session()
            if err is not None:
                return _j(err)
            res = rt.leases.acquire(
                holder=holder, purpose=purpose, ttl_seconds=ttl_seconds, force=force
            )
            if res.get("ok"):
                rt.overlay.sync_visibility(visible=True)
            res["overlay"] = rt.overlay.status()
            return _j(res)
        if act == "renew":
            err = rt.guard.check_allowed()
            if err is not None:
                return _j(err)
            res = rt.leases.renew(holder=holder, ttl_seconds=ttl_seconds)
            if res.get("ok"):
                rt.overlay.sync_visibility(visible=True)
            res["overlay"] = rt.overlay.status()
            return _j(res)
        if act == "release":
            res = rt.leases.release(holder=holder, force=force)
            rt.overlay.sync_visibility(visible=rt.leases.status() is not None)
            res["overlay"] = rt.overlay.status()
            return _j(res)
        return _j(
            {"ok": False, "code": "INVALID_ACTION", "message": f"Unknown lease action: {action!r}"}
        )

    @mcp.tool(name="corner_failsafe")
    def corner_failsafe_tool(action: str = "status") -> str:
        if action.strip().lower() != "status":
            return _j(
                {
                    "ok": False,
                    "code": "INVALID_ACTION",
                    "message": "Only action='status' is supported; re-acquire lease via lease_manage.",
                }
            )
        return _j({"ok": True, "failsafe": rt.guard.status(), "overlay": rt.overlay.status()})

    del lease_manage_tool, corner_failsafe_tool


def build_server(
    *, root: Path, pid: int, corner_px: int = CORNER_FAILSAFE_PX
) -> tuple[MCPServer, DesktopLeaseManager, BorderOverlay, CornerFailsafeGuard, str]:
    dpi_mode = init_dpi_awareness()
    leases = DesktopLeaseManager(root / "lease.json", now=_utc_now)
    overlay = BorderOverlay(should_show_fn=lambda: leases.status() is not None)

    def _on_corner_abort(_pos: tuple[int, int]) -> str | None:
        active = leases.status()
        prev_holder = str(active["holder"]) if active and active.get("holder") else None
        if active is not None:
            _ = leases.release(force=True)
        overlay.sync_visibility(visible=False)
        return prev_holder

    guard = CornerFailsafeGuard(
        now=_utc_now,
        on_abort=_on_corner_abort,
        has_active_session=lambda: leases.status() is not None,
        threshold_px=corner_px,
    )
    rt = _RuntimeHandles(
        pid=pid,
        dpi_mode=dpi_mode,
        shots_dir=root / "shots",
        now=_utc_now,
        leases=leases,
        overlay=overlay,
        guard=guard,
        uia=UiaWorker(monotonic=time.monotonic),
    )
    mcp = MCPServer("meiva-win-helper")
    _register_desktop_tools(mcp, rt)
    _register_input_tools(mcp, rt)
    _register_control_tools(mcp, rt)
    return mcp, leases, overlay, guard, dpi_mode


def main() -> None:
    root = Path(os.environ.get("MEIVA_WIN_BRIDGE_ROOT", r"D:\meiva-win-bridge"))
    port = int(os.environ.get("MEIVA_WIN_BRIDGE_PORT", str(DEFAULT_PORT)))
    corner_px = int(os.environ.get("MEIVA_CORNER_FAILSAFE_PX", str(CORNER_FAILSAFE_PX)))
    pid = os.getpid()

    logger = _setup_logging(root / "logs")
    token = _load_bearer_token(root / "secret.token")
    mcp, leases, overlay, guard, dpi_mode = build_server(root=root, pid=pid, corner_px=corner_px)
    sinfo = get_session_info(pid=pid)
    logger.info(
        "Starting meiva-win-helper on http://%s:%d/mcp (session=%s desktop=%s dpi=%s)",
        DEFAULT_HOST,
        port,
        sinfo["session_id"],
        sinfo["desktop_name"],
        dpi_mode,
    )
    overlay.start()
    guard.start()
    try:
        mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="")
        app = BearerAuthMiddleware(
            mcp_app,
            token,
            health_payload=lambda: {
                "ok": True,
                "session": get_session_info(pid=pid),
                "session_aborted": guard.status()["session_aborted"],
                "overlay_visible": overlay.status()["visible"],
                "lease_holder": (leases.status() or {}).get("holder"),
            },
        )
        uvicorn.run(
            app,
            host=DEFAULT_HOST,
            port=port,
            log_config=None,
            access_log=False,
            lifespan="on",
        )
    finally:
        guard.stop()
        overlay.stop()


if __name__ == "__main__":
    main()
