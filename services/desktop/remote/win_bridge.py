from __future__ import annotations

import itertools
from collections.abc import Mapping
from pathlib import Path

import httpx

from base.clock import Timers
from base.host.boundary.fields import optional_float_field, optional_int_field
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.permissions_helper.client import PermissionsHelperError
from base.host.permissions_helper.wire import (
    AppInfo,
    AppsResult,
    AxActResult,
    AxTreeResult,
    AxWindowInfo,
    ClickPoint,
    ClickResult,
    ClipboardReadResult,
    ClipboardWriteResult,
    DragResult,
    FocusAppResult,
    FrontmostApp,
    IndicatorStopResult,
    KeyResult,
    MouseDownResult,
    MouseReleaseAllResult,
    MouseUpResult,
    MoveResult,
    PingResult,
    ScreenSize,
    ScrollResult,
    SessionInfo,
    TypeResult,
    WindowCaptureResult,
    WindowInfo,
    WindowRow,
    WindowsResult,
)

from .errors import CornerFailsafeError
from .plan import (
    CORNER_FAILSAFE_CODE,
    extract_mcp_tool_payload,
    macos_keycode_to_win_key,
    map_ax_act_to_win_action,
    mime_to_extension,
    parse_streamable_http_body,
)
from .uia_map import win_uia_to_ax_tree

_DEFAULT_TIMEOUT_S = 30.0
_MIN_REMOTE_LEASE_SECONDS = 30
_MAX_REMOTE_LEASE_SECONDS = 3600
_DEFAULT_REMOTE_LEASE_SECONDS = 600
_HEALTHZ_TIMEOUT_S = 2.0
_MEIVA_HOLDER_PREFIX = "meiva-agent-"


def _float_pair(raw: object) -> tuple[float, float] | None:
    if not is_object_list(raw) or len(raw) != 2:
        return None
    a, b = raw[0], raw[1]
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    return float(a), float(b)


def _int_pair(raw: object) -> tuple[int, int] | None:
    pair = _float_pair(raw)
    if pair is None:
        return None
    return round(pair[0]), round(pair[1])


def _window_row_from_entry(w: dict[str, object], needle: str) -> WindowRow | None:
    proc = str(w.get("process") or "")
    title = str(w.get("title") or "")
    if needle and needle not in proc.lower() and needle not in title.lower():
        return None
    rect = _float_rect(w.get("rect"))
    rx, ry, rw, rh = rect if rect is not None else (0.0, 0.0, 0.0, 0.0)
    return {
        "window_id": optional_int_field(w, "hwnd") or 0,
        "pid": optional_int_field(w, "pid") or 0,
        "owner": proc or None,
        "title": title or None,
        "on_screen": not bool(w.get("is_minimized", False)),
        "x": rx,
        "y": ry,
        "w": rw,
        "h": rh,
    }


def _float_rect(raw: object) -> tuple[float, float, float, float] | None:
    if not is_object_list(raw) or len(raw) != 4:
        return None
    vals: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        vals.append(float(item))
    return vals[0], vals[1], vals[2], vals[3]


def probe_win_bridge_health(
    base_url: str,
    *,
    timeout_s: float = _HEALTHZ_TIMEOUT_S,
    client: httpx.Client | None = None,
) -> dict[str, object] | None:
    url = f"{base_url.rstrip('/')}/healthz"
    own = client is None
    http = client if client is not None else httpx.Client(timeout=timeout_s, trust_env=False)
    try:
        resp = http.get(url)
        if resp.status_code != 200:
            return None
        parsed = object_of(resp.json)
        return parsed if is_object_dict(parsed) else None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            http.close()


class RemoteWinBridgeHelper:
    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        holder: str = "meiva-agent",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        lease_seconds: int = _DEFAULT_REMOTE_LEASE_SECONDS,
        timers: Timers | None = None,
        allow_meiva_handoff: bool = True,
        handoff_prefix: str = _MEIVA_HOLDER_PREFIX,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._holder = holder
        self._timeout_s = timeout_s
        self._lease_seconds = max(
            _MIN_REMOTE_LEASE_SECONDS, min(_MAX_REMOTE_LEASE_SECONDS, int(lease_seconds))
        )
        self._timers = timers
        self._allow_meiva_handoff = allow_meiva_handoff
        self._handoff_prefix = handoff_prefix
        self._client = client
        self._req_ids = itertools.count(1)
        self._lease_acquired = False
        self._lease_expires_at = 0.0
        self._uia_id_map: dict[int, str] = {}

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def holder(self) -> str:
        return self._holder

    @property
    def handoff_prefix(self) -> str:
        return self._handoff_prefix

    def set_holder(self, holder: str) -> None:
        if holder != self._holder:
            self._holder = holder
            self._lease_acquired = False
            self._lease_expires_at = 0.0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    def _call_mcp(
        self, tool: str, arguments: Mapping[str, object] | None = None
    ) -> tuple[dict[str, object], bytes | None, str | None]:
        rpc_req = {
            "jsonrpc": "2.0",
            "id": next(self._req_ids),
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments or {})},
        }
        url = f"{self._base_url}/mcp"
        own = self._client is None
        http = (
            self._client
            if self._client is not None
            else httpx.Client(timeout=self._timeout_s, trust_env=False)
        )
        try:
            resp = http.post(url, json=rpc_req, headers=self._headers())
        except httpx.HTTPError as exc:
            raise PermissionsHelperError(
                f"remote Windows desktop helper unreachable at {self._base_url}: {exc}"
            ) from exc
        finally:
            if own:
                http.close()

        if resp.status_code == 401:
            raise PermissionsHelperError(
                "remote Windows desktop helper rejected Bearer token (HTTP 401)"
            )
        if resp.status_code >= 400:
            raise PermissionsHelperError(
                f"remote Windows desktop helper returned HTTP {resp.status_code}"
            )
        try:
            ct_val = object_of(resp.headers.get, "content-type", "")
            parsed = parse_streamable_http_body(resp.content, str(ct_val))
        except ValueError as exc:
            raise PermissionsHelperError(str(exc)) from exc
        extracted = extract_mcp_tool_payload(parsed)
        if extracted.error_code == CORNER_FAILSAFE_CODE:
            self._lease_acquired = False
            raise CornerFailsafeError(
                extracted.error_message or "corner failsafe triggered",
                code=CORNER_FAILSAFE_CODE,
            )
        if extracted.error_message is not None:
            if extracted.error_code:
                raise PermissionsHelperError(f"{extracted.error_code}: {extracted.error_message}")
            raise PermissionsHelperError(extracted.error_message)
        return extracted.data, extracted.image_bytes, extracted.mime_type

    def _remote_lease_holder(self) -> str | None:
        try:
            payload, _, _ = self._call_mcp("lease_manage", {"action": "status"})
        except PermissionsHelperError:
            return None
        lease = payload.get("lease")
        if is_object_dict(lease) and isinstance(lease.get("holder"), str):
            return str(lease["holder"])
        return None

    def _mark_lease_active(self) -> None:
        self._lease_acquired = True
        if self._timers is not None:
            self._lease_expires_at = self._timers.monotonic() + float(self._lease_seconds)

    def acquire_remote_lease(self, *, ttl_seconds: int | None = None) -> dict[str, object]:
        ttl = (
            max(_MIN_REMOTE_LEASE_SECONDS, min(_MAX_REMOTE_LEASE_SECONDS, int(ttl_seconds)))
            if ttl_seconds is not None
            else self._lease_seconds
        )
        req: dict[str, object] = {
            "action": "acquire",
            "holder": self._holder,
            "purpose": "meiva computer_use session",
            "ttl_seconds": ttl,
        }
        try:
            payload, _, _ = self._call_mcp("lease_manage", req)
        except PermissionsHelperError as exc:
            if not self._allow_meiva_handoff or "DESKTOP_LEASE_HELD" not in str(exc):
                raise
            current_holder = self._remote_lease_holder()
            if (
                current_holder is None
                or current_holder == self._holder
                or not current_holder.startswith(self._handoff_prefix)
            ):
                raise
            payload, _, _ = self._call_mcp("lease_manage", {**req, "force": True})
        self._lease_seconds = ttl
        self._mark_lease_active()
        return payload

    def _ensure_remote_lease(self) -> None:
        if self._lease_acquired:
            if self._timers is None or self._timers.monotonic() < self._lease_expires_at:
                return
            self._lease_acquired = False
            self._lease_expires_at = 0.0
        _ = self.acquire_remote_lease()

    def release_remote_lease(self, *, force: bool = False) -> bool:
        self._lease_acquired = False
        self._lease_expires_at = 0.0
        payload, _, _ = self._call_mcp(
            "lease_manage",
            {"action": "release", "holder": self._holder, "force": force},
        )
        return bool(payload.get("released", False))

    def ping(self) -> PingResult:
        health = probe_win_bridge_health(self._base_url, client=self._client)
        if health is None:
            raise PermissionsHelperError(
                f"remote Windows desktop helper /healthz failed at {self._base_url}"
            )
        session = health.get("session")
        interactive = (
            bool(session.get("desktop_interactive", True)) if is_object_dict(session) else True
        )
        return {
            "pong": bool(health.get("ok", True)),
            "preflight_screen": interactive,
            "ax_trusted": interactive,
            "ax_tree_v1": True,
            "ax_act_v1": True,
            "ax_act_v2": False,
            "ax_target_v1": True,
            "native_input_v1": True,
            "corner_failsafe_v1": True,
            "capture_indicator_v1": True,
            "clipboard_v1": True,
            "mouse_hold_v1": False,
        }

    def screen_size(self) -> ScreenSize:
        data, _, _ = self._call_mcp("status")
        screen = data.get("screen")
        monitors = screen.get("monitors") if is_object_dict(screen) else None
        if is_object_list(monitors):
            primary = next(
                (m for m in monitors if is_object_dict(m) and m.get("role") == "primary"),
                monitors[0] if monitors else None,
            )
            if is_object_dict(primary):
                return {
                    "x": float(optional_float_field(primary, "left") or 0.0),
                    "y": float(optional_float_field(primary, "top") or 0.0),
                    "w": float(optional_float_field(primary, "width") or 1920.0),
                    "h": float(optional_float_field(primary, "height") or 1080.0),
                    "scale": 1.0,
                }
        raise PermissionsHelperError("remote Windows desktop helper reported no monitor geometry")

    def screencapture_region(self, x: int, y: int, w: int, h: int, path: str) -> dict[str, object]:
        meta, img_bytes, mime = self._call_mcp(
            "screenshot",
            {
                "x": x,
                "y": y,
                "width": w,
                "height": h,
                "format": "webp",
                "quality": 75,
                "max_width": 0,
            },
        )
        if not img_bytes:
            raise PermissionsHelperError("remote Windows desktop helper returned no image bytes")
        resolved_mime = mime or str(meta.get("mime_type") or "image/webp")
        dst = Path(path).with_suffix(mime_to_extension(resolved_mime))
        dst.parent.mkdir(parents=True, exist_ok=True)
        _ = dst.write_bytes(img_bytes)
        out: dict[str, object] = {
            "path": str(dst),
            "bytes": len(img_bytes),
            "mime_type": resolved_mime,
        }
        for key in ("capture_rect", "scale_x", "scale_y", "image_size"):
            if key in meta:
                out[key] = meta[key]
        return out

    def screencapture_window(self, pid: int, window_id: int, path: str) -> WindowCaptureResult:
        del pid
        meta, img_bytes, mime = self._call_mcp(
            "screenshot",
            {"hwnd": window_id, "format": "webp", "quality": 75, "max_width": 0},
        )
        if not img_bytes:
            raise PermissionsHelperError("remote Windows desktop helper returned no image bytes")
        resolved_mime = mime or str(meta.get("mime_type") or "image/webp")
        dst = Path(path).with_suffix(mime_to_extension(resolved_mime))
        dst.parent.mkdir(parents=True, exist_ok=True)
        _ = dst.write_bytes(img_bytes)
        rect = _float_rect(meta.get("capture_rect"))
        ox, oy = (rect[0], rect[1]) if rect is not None else (0.0, 0.0)
        dims = _int_pair(meta.get("image_size"))
        iw, ih = dims if dims is not None else (0, 0)
        return {
            "path": str(dst),
            "bytes": len(img_bytes),
            "origin": {"x": ox, "y": oy},
            "scale": 1.0,
            "width": iw,
            "height": ih,
        }

    def frontmost_app(self) -> FrontmostApp:
        data, _, _ = self._call_mcp("status")
        fg = data.get("foreground_window")
        if is_object_dict(fg):
            return {"app": str(fg.get("process") or fg.get("title") or "")}
        return {"app": ""}

    def cursor_position(self) -> ClickPoint:
        data, _, _ = self._call_mcp("corner_failsafe", {"action": "status"})
        fs = data.get("failsafe")
        pair = _float_pair(fs.get("cursor_position")) if is_object_dict(fs) else None
        if pair is not None:
            return {"x": pair[0], "y": pair[1]}
        return {"x": 0.0, "y": 0.0}

    def click(
        self,
        x: float,
        y: float,
        *,
        double: bool = False,
        button: str = "left",
        click_count: int | None = None,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> ClickResult:
        del duration_ms
        self._ensure_remote_lease()
        clicks = click_count if click_count is not None else (2 if double else 1)
        _ = self._call_mcp(
            "mouse_click",
            {
                "x": round(x),
                "y": round(y),
                "button": button,
                "clicks": clicks,
                "modifiers": "+".join(modifiers or ()),
                "holder": self._holder,
            },
        )
        return {
            "clicked": {"x": float(x), "y": float(y)},
            "double": clicks == 2,
            "button": button,
            "click_count": clicks,
        }

    def move(self, x: float, y: float, *, modifiers: list[str] | None = None) -> MoveResult:
        del modifiers
        self._ensure_remote_lease()
        _ = self._call_mcp(
            "mouse_scroll",
            {"x": round(x), "y": round(y), "dy": 0, "dx": 0, "holder": self._holder},
        )
        return {"moved": {"x": float(x), "y": float(y)}}

    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        button: str | None = None,
        modifiers: list[str] | None = None,
    ) -> DragResult:
        del modifiers
        self._ensure_remote_lease()
        btn = button or "left"
        _ = self._call_mcp(
            "mouse_move_drag",
            {
                "start_x": round(start_x),
                "start_y": round(start_y),
                "end_x": round(end_x),
                "end_y": round(end_y),
                "button": btn,
                "holder": self._holder,
            },
        )
        return {
            "start": {"x": float(start_x), "y": float(start_y)},
            "end": {"x": float(end_x), "y": float(end_y)},
            "button": btn,
        }

    def mouse_down(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> MouseDownResult:
        del x, y, button, modifiers
        raise PermissionsHelperError(
            "mouse_down is not supported on Windows remote helper; use drag() instead"
        )

    def mouse_up(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
    ) -> MouseUpResult:
        del x, y, button
        raise PermissionsHelperError(
            "mouse_up is not supported on Windows remote helper; use drag() instead"
        )

    def mouse_release_all(self) -> MouseReleaseAllResult:
        return {"released": []}

    def type_text(self, text: str) -> TypeResult:
        self._ensure_remote_lease()
        data, _, _ = self._call_mcp("type_text", {"text": text, "holder": self._holder})
        typed = optional_int_field(data, "chars")
        return {"typed": typed if typed is not None else len(text)}

    def key(
        self,
        code: int,
        *,
        cmd: bool = False,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> KeyResult:
        del duration_ms
        self._ensure_remote_lease()
        chord = macos_keycode_to_win_key(code, cmd=cmd, modifiers=modifiers)
        if chord is None:
            raise PermissionsHelperError(
                f"unsupported virtual keycode {code} for Windows remote host; pass a key name"
            )
        _ = self._call_mcp("press_key", {"keys": chord, "holder": self._holder})
        return {"key": code, "cmd": cmd}

    def scroll(
        self,
        x: float,
        y: float,
        dy: int,
        *,
        dx: int | None = None,
        modifiers: list[str] | None = None,
    ) -> ScrollResult:
        del modifiers
        self._ensure_remote_lease()
        dx_val = dx or 0
        _ = self._call_mcp(
            "mouse_scroll",
            {
                "x": round(x),
                "y": round(y),
                "dy": dy,
                "dx": dx_val,
                "holder": self._holder,
            },
        )
        return {"scrolled": dy, "dx": dx_val}

    def clipboard_read(self) -> ClipboardReadResult:
        data, _, _ = self._call_mcp("clipboard_get")
        raw_text = data.get("text")
        text = str(raw_text) if isinstance(raw_text, str) else None
        length = optional_int_field(data, "length") or (len(text) if text else 0)
        return {
            "text": text,
            "types": ["text/plain"] if text is not None else [],
            "length": length,
            "truncated": bool(data.get("truncated", False)),
        }

    def clipboard_write(self, text: str) -> ClipboardWriteResult:
        self._ensure_remote_lease()
        data, _, _ = self._call_mcp("clipboard_set", {"text": text, "holder": self._holder})
        length = optional_int_field(data, "length")
        return {"length": length if length is not None else len(text)}

    def indicator_stop(self) -> IndicatorStopResult:
        return {"stopped": self.release_remote_lease(force=False)}

    def list_apps(self) -> AppsResult:
        data, _, _ = self._call_mcp("list_windows", {"visible_only": True})
        wins = data.get("windows")
        seen: set[int] = set()
        apps: list[AppInfo] = []
        if is_object_list(wins):
            for w in wins:
                if not is_object_dict(w):
                    continue
                pid = optional_int_field(w, "pid")
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                proc = str(w.get("process") or "") or None
                apps.append({"pid": pid, "name": proc, "bundle_id": proc})
        return {"apps": apps}

    def list_windows(self, app: str | None = None) -> WindowsResult:
        data, _, _ = self._call_mcp("list_windows", {"visible_only": True})
        wins = data.get("windows")
        rows: list[WindowRow] = []
        needle = app.strip().lower() if app else ""
        if is_object_list(wins):
            for w in wins:
                if not is_object_dict(w):
                    continue
                row = _window_row_from_entry(w, needle)
                if row is not None:
                    rows.append(row)
        return {"windows": rows}

    def _resolve_window_selector(
        self, target: dict[str, object], *, require_pid_match: bool
    ) -> tuple[int, str, int]:
        pid = optional_int_field(target, "pid")
        if pid is not None:
            wins = self.list_windows()["windows"]
            match = next((w for w in wins if w["pid"] == pid), None)
            if match is None:
                if require_pid_match:
                    raise PermissionsHelperError(f"no visible window found for pid={pid}")
                return 0, "", pid
            return match["window_id"], "", pid
        bundle = target.get("bundle_id")
        if isinstance(bundle, str) and bundle.strip():
            needle = bundle.strip()
            wins = self.list_windows(needle)["windows"]
            if wins:
                exact = next(
                    (w for w in wins if (w["owner"] or "").lower() == needle.lower()),
                    wins[0],
                )
                return exact["window_id"], "", exact["pid"]
            return 0, needle, 0
        return 0, "", 0

    def focus_app(self, target: dict[str, object]) -> FocusAppResult:
        self._ensure_remote_lease()
        hwnd, title_contains, resolved_pid = self._resolve_window_selector(
            target, require_pid_match=True
        )
        data, _, _ = self._call_mcp(
            "focus_window",
            {"hwnd": hwnd, "title_contains": title_contains, "holder": self._holder},
        )
        return {"focused": bool(data.get("is_foreground", True)), "pid": resolved_pid}

    def ax_window_info(self, app: str) -> AxWindowInfo:
        wins = self.list_windows(app)["windows"]
        if not wins:
            raise PermissionsHelperError(f"no window found for app {app!r}")
        w = wins[0]
        return {"app": app, "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"]}

    def window_info(self, owner: str) -> WindowInfo:
        wins = self.list_windows(owner)["windows"]
        if not wins:
            raise PermissionsHelperError(f"no window found for owner {owner!r}")
        w = wins[0]
        return {"owner": owner, "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"]}

    def session_info(self) -> SessionInfo:
        data, _, _ = self._call_mcp("status")
        session = data.get("session")
        interactive = (
            bool(session.get("desktop_interactive", True)) if is_object_dict(session) else True
        )
        return {"locked": not interactive, "on_console": interactive}

    def ax_tree(
        self,
        app: str | None = None,
        *,
        target: dict[str, object] | None = None,
        scope: int | None = None,
        scope_fp: str | None = None,
        max_nodes: int = 600,
        max_depth: int = 14,
        budget_ms: int = 1500,
        timeout_ms: int = 400,
        enable_ax: bool = True,
    ) -> AxTreeResult:
        del scope, scope_fp, timeout_ms, enable_ax
        hwnd = 0
        title_contains = ""
        if target is not None:
            hwnd, title_contains, _ = self._resolve_window_selector(target, require_pid_match=False)
        elif app:
            wins = self.list_windows(app)["windows"]
            if wins:
                hwnd = wins[0]["window_id"]
            else:
                title_contains = app
        data, _, _ = self._call_mcp(
            "uia_tree",
            {
                "hwnd": hwnd,
                "title_contains": title_contains,
                "mode": "interactive",
                "max_depth": min(12, max_depth),
                "max_nodes": min(400, max_nodes),
                "timeout_sec": max(1.0, budget_ms / 1000.0),
            },
        )
        ax_result, id_map = win_uia_to_ax_tree(data)
        self._uia_id_map = id_map
        return ax_result

    def ax_act(
        self,
        app: str,
        raw_id: int,
        action: str,
        *,
        pid: int | None = None,
        value: str | None = None,
        native_action: str | None = None,
        text: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        selection_type: str | None = None,
        timeout_ms: int = 2000,
    ) -> AxActResult:
        del app, pid, native_action, text, prefix, suffix, selection_type, timeout_ms
        self._ensure_remote_lease()
        eid = self._uia_id_map.get(raw_id, f"e{raw_id}")
        data, _, _ = self._call_mcp(
            "uia_act",
            {
                "element_id": eid,
                "action": map_ax_act_to_win_action(action),
                "value": value or "",
                "holder": self._holder,
            },
        )
        out: AxActResult = {"completed": bool(data.get("ok", True)), "action": action}
        clicked = _float_pair(data.get("clicked"))
        if clicked is not None:
            out["x"] = clicked[0]
            out["y"] = clicked[1]
            out["w"] = 0.0
            out["h"] = 0.0
        return out
