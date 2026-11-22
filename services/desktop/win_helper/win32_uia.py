from __future__ import annotations

import ctypes
import io
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import psutil
from PIL import Image

from .uia_logic import CONTAINER_TYPES, INTERACTIVE_TYPES, TEXT_TYPES, classify_tree_quality
from .win32_ffi import (
    DWORD,
    HWND,
    RECT,
    as_int,
    is_object_seq,
    is_windows,
    make_enumproc,
    object_of,
    win_dll,
)
from .win32_input import mouse_click, press_key, type_text

SW_RESTORE = 9
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
MAX_SAVED_SHOTS = 30


class _UiaRect(Protocol):
    @property
    def left(self) -> int: ...
    @property
    def top(self) -> int: ...
    @property
    def right(self) -> int: ...
    @property
    def bottom(self) -> int: ...


class _UiaPattern(Protocol):
    @property
    def Value(self) -> str: ...
    @property
    def ToggleState(self) -> int: ...
    def Invoke(self) -> None: ...
    def SetValue(self, value: str) -> None: ...
    def Toggle(self) -> None: ...
    def Select(self) -> None: ...
    def Expand(self) -> None: ...
    def Collapse(self) -> None: ...


class _UiaControl(Protocol):
    @property
    def ControlTypeName(self) -> str: ...
    @property
    def Name(self) -> str: ...
    @property
    def AutomationId(self) -> str: ...
    @property
    def IsOffscreen(self) -> bool: ...
    @property
    def BoundingRectangle(self) -> _UiaRect | None: ...
    def GetPattern(self, pattern_id: int, /) -> _UiaPattern | None: ...
    def GetChildren(self) -> Sequence[_UiaControl]: ...
    def SetFocus(self) -> None: ...


@dataclass
class _SnapshotElement:
    element_id: str
    hwnd: int
    role: str
    name: str
    automation_id: str
    rect: list[int]
    patterns: list[str]
    control: _UiaControl


def _window_rect(hwnd: int) -> list[int]:
    if not is_windows():
        return [0, 0, 0, 0]
    rect = RECT()
    with suppress(OSError, AttributeError, RuntimeError):
        hr = as_int(
            object_of(
                win_dll("dwmapi").DwmGetWindowAttribute,
                HWND(hwnd),
                DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
                ctypes.byref(rect),
                ctypes.sizeof(rect),
            ),
            1,
        )
        if hr == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return [
                int(rect.left),
                int(rect.top),
                int(rect.right - rect.left),
                int(rect.bottom - rect.top),
            ]
    if object_of(win_dll("user32").GetWindowRect, HWND(hwnd), ctypes.byref(rect)):
        return [
            int(rect.left),
            int(rect.top),
            int(rect.right - rect.left),
            int(rect.bottom - rect.top),
        ]
    return [0, 0, 0, 0]


def _is_cloaked(hwnd: int) -> bool:
    if not is_windows():
        return False
    try:
        cloaked = DWORD(0)
        hr = as_int(
            object_of(
                win_dll("dwmapi").DwmGetWindowAttribute,
                HWND(hwnd),
                DWORD(DWMWA_CLOAKED),
                ctypes.byref(cloaked),
                ctypes.sizeof(cloaked),
            ),
            1,
        )
        return hr == 0 and bool(cloaked.value)
    except (OSError, AttributeError, RuntimeError):
        return False


def _window_title(hwnd: int) -> str:
    if not is_windows():
        return ""
    user32 = win_dll("user32")
    length = as_int(object_of(user32.GetWindowTextLengthW, HWND(hwnd)))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _ = object_of(user32.GetWindowTextW, HWND(hwnd), buf, length + 1)
    return ctypes.wstring_at(buf)


def _window_pid(hwnd: int) -> int:
    if not is_windows():
        return 0
    pid = DWORD(0)
    _ = object_of(win_dll("user32").GetWindowThreadProcessId, HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _proc_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):
        return ""


def list_windows(
    *, visible_only: bool = True, title_contains: str = "", exclude_hwnd: int = 0
) -> list[dict[str, object]]:
    if not is_windows():
        return []
    user32 = win_dll("user32")
    fg = as_int(object_of(user32.GetForegroundWindow))
    needle = title_contains.strip().lower()
    results: list[dict[str, object]] = []

    def cb(hwnd_val: int, _lparam: int) -> int:
        hwnd = int(hwnd_val)
        if exclude_hwnd and hwnd == exclude_hwnd:
            return 1
        if visible_only and (
            not bool(object_of(user32.IsWindowVisible, hwnd)) or _is_cloaked(hwnd)
        ):
            return 1
        title = _window_title(hwnd).strip()
        if visible_only and not title:
            return 1
        if needle and needle not in title.lower():
            return 1
        rect = _window_rect(hwnd)
        is_min = bool(object_of(user32.IsIconic, hwnd))
        if visible_only and not is_min and (rect[2] <= 1 or rect[3] <= 1):
            return 1
        pid = _window_pid(hwnd)
        results.append(
            {
                "hwnd": hwnd,
                "pid": pid,
                "process": _proc_name(pid),
                "title": title,
                "rect": rect,
                "is_foreground": hwnd == fg,
                "is_minimized": is_min,
            }
        )
        return 1

    _ = object_of(user32.EnumWindows, make_enumproc(cb), 0)
    results.sort(
        key=lambda w: (
            not bool(w["is_foreground"]),
            bool(w["is_minimized"]),
            str(w["title"]).lower(),
        )
    )
    return results


def resolve_window(hwnd: int = 0, title_contains: str = "") -> dict[str, object]:
    if not is_windows():
        raise RuntimeError("Window resolution is only supported on Windows")
    user32 = win_dll("user32")
    if hwnd and int(hwnd) > 0:
        h = int(hwnd)
        if not object_of(user32.IsWindow, HWND(h)):
            raise ValueError(f"Window handle {h} does not exist")
        pid = _window_pid(h)
        return {
            "hwnd": h,
            "pid": pid,
            "process": _proc_name(pid),
            "title": _window_title(h),
            "rect": _window_rect(h),
            "is_foreground": h == as_int(object_of(user32.GetForegroundWindow)),
            "is_minimized": bool(object_of(user32.IsIconic, HWND(h))),
        }
    if title_contains.strip():
        matches = list_windows(visible_only=True, title_contains=title_contains) or list_windows(
            visible_only=False, title_contains=title_contains
        )
        if not matches:
            raise ValueError(f"No window found matching title_contains={title_contains!r}")
        return matches[0]
    fg = as_int(object_of(user32.GetForegroundWindow))
    if fg <= 0:
        wins = list_windows(visible_only=True)
        if not wins:
            raise ValueError("No visible foreground window found (is desktop locked?)")
        return wins[0]
    pid = _window_pid(fg)
    return {
        "hwnd": fg,
        "pid": pid,
        "process": _proc_name(pid),
        "title": _window_title(fg),
        "rect": _window_rect(fg),
        "is_foreground": True,
        "is_minimized": bool(object_of(user32.IsIconic, HWND(fg))),
    }


def focus_window(hwnd: int = 0, title_contains: str = "") -> dict[str, object]:
    if not is_windows():
        raise RuntimeError("focus_window is only supported on Windows")
    target = resolve_window(hwnd=hwnd, title_contains=title_contains)
    h = as_int(target["hwnd"])
    user32, kernel32 = win_dll("user32"), win_dll("kernel32")
    if object_of(user32.IsIconic, HWND(h)):
        _ = object_of(user32.ShowWindow, HWND(h), SW_RESTORE)
        time.sleep(0.08)
    fg = as_int(object_of(user32.GetForegroundWindow))
    if fg != h:
        fg_tid = as_int(object_of(user32.GetWindowThreadProcessId, HWND(fg), None)) if fg else 0
        cur_tid = as_int(object_of(kernel32.GetCurrentThreadId))
        attached = (
            bool(object_of(user32.AttachThreadInput, cur_tid, fg_tid, 1))
            if fg_tid and fg_tid != cur_tid
            else False
        )
        try:
            _ = object_of(user32.BringWindowToTop, HWND(h))
            if not object_of(user32.SetForegroundWindow, HWND(h)):
                _ = press_key("Alt")
                time.sleep(0.03)
                _ = object_of(user32.SetForegroundWindow, HWND(h))
        finally:
            if attached:
                _ = object_of(user32.AttachThreadInput, cur_tid, fg_tid, 0)
        time.sleep(0.08)
    now_fg = as_int(object_of(user32.GetForegroundWindow))
    return {
        "ok": True,
        "hwnd": h,
        "title": _window_title(h),
        "rect": _window_rect(h),
        "is_foreground": now_fg == h,
    }


def get_screen_geometry() -> dict[str, object]:
    if not is_windows():
        return {"monitors": [], "system_dpi": 96, "scale_percent": 100}
    import mss

    with mss.mss() as sct:
        monitors: list[dict[str, object]] = [
            {
                "index": idx,
                "role": "virtual_all"
                if idx == 0
                else ("primary" if idx == 1 else f"display_{idx}"),
                "left": int(mon["left"]),
                "top": int(mon["top"]),
                "width": int(mon["width"]),
                "height": int(mon["height"]),
            }
            for idx, mon in enumerate(sct.monitors)
        ]
    dpi = 96
    with suppress(OSError, AttributeError, RuntimeError):
        dpi = as_int(object_of(win_dll("user32").GetDpiForSystem), 96)
    return {"monitors": monitors, "system_dpi": dpi, "scale_percent": round(dpi * 100 / 96)}


def _should_emit_node(
    *, depth: int, mode: str, name: str, auto_id: str, ctype: str, width: int
) -> bool:
    if depth == 0:
        return True
    is_inter = ctype in INTERACTIVE_TYPES
    has_label = bool(name or auto_id)
    if mode == "full":
        return has_label or is_inter or width > 0
    if mode == "text":
        return (ctype in TEXT_TYPES or is_inter) and has_label
    if is_inter:
        return True
    if not name:
        return False
    return (ctype in CONTAINER_TYPES and depth <= 3) or (ctype in TEXT_TYPES and depth <= 4)


def _read_control_info(ctrl: _UiaControl) -> tuple[str, str, str, bool, list[int]] | None:
    try:
        ctype = str(ctrl.ControlTypeName or "Unknown")
        name = str(ctrl.Name or "").replace("\r", " ").replace("\n", " ").strip()
        auto_id = str(ctrl.AutomationId or "").strip()
        is_offscreen = bool(ctrl.IsOffscreen)
        br = ctrl.BoundingRectangle
        rect = (
            [int(br.left), int(br.top), int(br.right - br.left), int(br.bottom - br.top)]
            if br and (br.right > br.left and br.bottom > br.top)
            else [0, 0, 0, 0]
        )
    except (OSError, RuntimeError, AttributeError, ValueError):
        return None
    return ctype, name, auto_id, is_offscreen, rect


def _element_payload(snap: _SnapshotElement, val_text: str) -> dict[str, object]:
    el_dict: dict[str, object] = {
        "id": snap.element_id,
        "role": snap.role,
        "name": snap.name,
        "rect": snap.rect,
    }
    if snap.automation_id:
        el_dict["automation_id"] = snap.automation_id
    if snap.patterns:
        el_dict["patterns"] = snap.patterns
    if val_text and val_text != snap.name:
        el_dict["value"] = val_text
    return el_dict


def _format_tree_line(depth: int, snap: _SnapshotElement, val_text: str) -> str:
    indent = "  " * min(depth, 8)
    label = f' "{snap.name}"' if snap.name else ""
    aid_s = f" #{snap.automation_id}" if snap.automation_id else ""
    pat_s = f" ({','.join(snap.patterns)})" if snap.patterns else ""
    val_s = f' val="{val_text}"' if (val_text and val_text != snap.name) else ""
    r = snap.rect
    return f"{indent}{snap.element_id} {snap.role}{label}{aid_s} [{r[0]},{r[1]},{r[2]},{r[3]}]{pat_s}{val_s}"


class UiaWorker:
    def __init__(self, *, monotonic: Callable[[], float]) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia-com")
        self._tls = threading.local()
        self._monotonic = monotonic
        self._snapshot: dict[str, _SnapshotElement] = {}

    def uia_tree(
        self,
        *,
        hwnd: int = 0,
        title_contains: str = "",
        mode: str = "interactive",
        max_depth: int = 8,
        max_nodes: int = 150,
        timeout_sec: float = 6.0,
    ) -> dict[str, object]:
        return self._executor.submit(
            self._uia_tree_sync, hwnd, title_contains, mode, max_depth, max_nodes, timeout_sec
        ).result(timeout=max(10.0, timeout_sec + 5.0))

    def uia_act(
        self, *, element_id: str, action: str = "invoke", value: str = ""
    ) -> dict[str, object]:
        return self._executor.submit(self._uia_act_sync, element_id, action, value).result(
            timeout=15.0
        )

    def _extract_patterns(self, ctrl: _UiaControl) -> tuple[list[str], str]:
        import uiautomation as auto

        patterns: list[str] = []
        val_text = ""
        pid_enum = auto.PatternId
        for pname, pat_id in (
            ("Invoke", pid_enum.InvokePattern),
            ("Value", pid_enum.ValuePattern),
            ("Toggle", pid_enum.TogglePattern),
            ("Select", pid_enum.SelectionItemPattern),
            ("Expand", pid_enum.ExpandCollapsePattern),
            ("Scroll", pid_enum.ScrollPattern),
        ):
            with suppress(OSError, RuntimeError, AttributeError):
                pat = ctrl.GetPattern(pat_id)
                if pat is not None:
                    patterns.append(pname)
                    if pname == "Value" and pat.Value:
                        val_text = pat.Value[:120]
        return patterns, val_text

    def _walk_controls(
        self,
        root: _UiaControl,
        *,
        target_hwnd: int,
        mode: str,
        max_depth: int,
        max_nodes: int,
        deadline: float,
    ) -> tuple[dict[str, _SnapshotElement], list[dict[str, object]], list[str], int, bool]:
        new_snapshot: dict[str, _SnapshotElement] = {}
        elements: list[dict[str, object]] = []
        lines: list[str] = []
        truncated = False
        interactive_count = 0
        seq = 0

        def emit_if_selected(
            ctrl: _UiaControl,
            depth: int,
            ctype: str,
            name: str,
            auto_id: str,
            rect: list[int],
        ) -> None:
            nonlocal interactive_count, seq
            if not _should_emit_node(
                depth=depth, mode=mode, name=name, auto_id=auto_id, ctype=ctype, width=rect[2]
            ):
                return
            is_inter = ctype in INTERACTIVE_TYPES
            patterns, val_text = (
                self._extract_patterns(ctrl) if (is_inter or ctype == "EditControl") else ([], "")
            )
            if is_inter or any(p in ("Invoke", "Toggle", "Select", "Value") for p in patterns):
                interactive_count += 1
            seq += 1
            eid = f"e{seq}"
            snap = _SnapshotElement(
                eid,
                target_hwnd,
                ctype.removesuffix("Control"),
                name[:120],
                auto_id[:80],
                rect,
                patterns,
                ctrl,
            )
            new_snapshot[eid] = snap
            elements.append(_element_payload(snap, val_text))
            lines.append(_format_tree_line(depth, snap, val_text))

        def walk(ctrl: _UiaControl, depth: int) -> None:
            nonlocal truncated
            if self._monotonic() > deadline or len(elements) >= max_nodes:
                truncated = True
                return
            info = _read_control_info(ctrl)
            if info is None:
                return
            ctype, name, auto_id, is_offscreen, rect = info
            if depth > 0 and is_offscreen and rect[2] == 0:
                return
            emit_if_selected(ctrl, depth, ctype, name, auto_id, rect)
            if depth >= max_depth:
                return
            try:
                children = ctrl.GetChildren()
            except (OSError, RuntimeError, AttributeError):
                return
            for child in children:
                if self._monotonic() > deadline or len(elements) >= max_nodes:
                    truncated = True
                    break
                walk(child, depth + 1)

        walk(root, 0)
        return new_snapshot, elements, lines, interactive_count, truncated

    def _uia_tree_sync(
        self,
        hwnd: int,
        title_contains: str,
        mode: str,
        max_depth: int,
        max_nodes: int,
        timeout_sec: float,
    ) -> dict[str, object]:
        if not is_windows():
            raise RuntimeError("UIAutomation is only supported on Windows")
        import uiautomation as auto

        if not getattr(self._tls, "initialized", False):
            self._tls.initializer = auto.UIAutomationInitializerInThread()
            auto.SetGlobalSearchTimeout(1.0)
            self._tls.initialized = True
        t0 = self._monotonic()
        win = resolve_window(hwnd=hwnd, title_contains=title_contains)
        target_hwnd = as_int(win["hwnd"])
        mode = (
            mode.strip().lower()
            if mode.strip().lower() in ("interactive", "text", "full")
            else "interactive"
        )
        root = auto.ControlFromHandle(target_hwnd)
        if root is None:
            raise RuntimeError(f"UIAutomation ControlFromHandle failed for hwnd={target_hwnd}")
        new_snapshot, elements, lines, interactive_count, truncated = self._walk_controls(
            root,
            target_hwnd=target_hwnd,
            mode=mode,
            max_depth=max(1, min(12, int(max_depth))),
            max_nodes=max(10, min(400, int(max_nodes))),
            deadline=t0 + max(1.0, min(15.0, float(timeout_sec))),
        )
        self._snapshot = new_snapshot
        win_rect = win["rect"]
        w_w = as_int(win_rect[2]) if is_object_seq(win_rect) and len(win_rect) >= 4 else 0
        w_h = as_int(win_rect[3]) if is_object_seq(win_rect) and len(win_rect) >= 4 else 0
        quality, hint = classify_tree_quality(
            window_width=w_w,
            window_height=w_h,
            element_count=len(elements),
            interactive_count=interactive_count,
        )
        return {
            "ok": True,
            "window": win,
            "mode": mode,
            "quality": quality,
            "hint": hint,
            "node_count": len(elements),
            "interactive_count": interactive_count,
            "truncated": truncated,
            "elapsed_ms": int((self._monotonic() - t0) * 1000),
            "tree": "\n".join(lines),
            "elements": elements,
        }

    def _act_invoke_or_value(
        self, snap: _SnapshotElement, act: str, value: str, cx: int, cy: int
    ) -> dict[str, object]:
        import uiautomation as auto

        ctrl, eid, rect, pid = snap.control, snap.element_id, snap.rect, auto.PatternId
        if act == "invoke":
            pat = ctrl.GetPattern(pid.InvokePattern)
            if pat is not None:
                pat.Invoke()
                return {"ok": True, "element_id": eid, "action": "invoke", "via": "InvokePattern"}
            if rect[2] > 0 and rect[3] > 0:
                _ = mouse_click(cx, cy)
                return {
                    "ok": True,
                    "element_id": eid,
                    "action": "invoke",
                    "via": "click_fallback",
                    "clicked": [cx, cy],
                }
            raise RuntimeError(f"Element {eid} has neither InvokePattern nor clickable rect")
        pat = ctrl.GetPattern(pid.ValuePattern)
        if pat is not None:
            pat.SetValue(value)
            return {"ok": True, "element_id": eid, "action": "set_value", "via": "ValuePattern"}
        if rect[2] > 0 and rect[3] > 0:
            _ = mouse_click(cx, cy)
            time.sleep(0.05)
            _ = type_text(value, clear_first=True)
            return {"ok": True, "element_id": eid, "action": "set_value", "via": "type_fallback"}
        raise RuntimeError(f"Element {eid} does not support ValuePattern or click focus")

    def _act_pattern(
        self, snap: _SnapshotElement, act: str, value: str, cx: int, cy: int
    ) -> dict[str, object]:
        import uiautomation as auto

        if act in ("invoke", "set_value"):
            return self._act_invoke_or_value(snap, act, value, cx, cy)
        ctrl, eid, pid = snap.control, snap.element_id, auto.PatternId
        if act == "toggle":
            pat = ctrl.GetPattern(pid.TogglePattern)
            if pat is None:
                raise RuntimeError(f"Element {eid} does not support TogglePattern")
            pat.Toggle()
            return {
                "ok": True,
                "element_id": eid,
                "action": "toggle",
                "state": int(pat.ToggleState),
            }
        if act == "select":
            pat = ctrl.GetPattern(pid.SelectionItemPattern)
            if pat is None:
                raise RuntimeError(f"Element {eid} does not support SelectionItemPattern")
            pat.Select()
            return {"ok": True, "element_id": eid, "action": "select"}
        if act in ("expand", "collapse"):
            pat = ctrl.GetPattern(pid.ExpandCollapsePattern)
            if pat is None:
                raise RuntimeError(f"Element {eid} does not support ExpandCollapsePattern")
            if act == "expand":
                pat.Expand()
            else:
                pat.Collapse()
            return {"ok": True, "element_id": eid, "action": act}
        raise ValueError(f"Unsupported action={act!r}")

    def _uia_act_sync(self, element_id: str, action: str, value: str) -> dict[str, object]:
        if not is_windows():
            raise RuntimeError("UIAutomation is only supported on Windows")
        eid = element_id.strip()
        snap = self._snapshot.get(eid)
        if snap is None:
            raise ValueError(
                f"Unknown element_id={eid!r}. Call uia_tree() first to refresh element IDs."
            )
        act = action.strip().lower()
        rect = snap.rect
        cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        if act in ("click", "double_click", "right_click"):
            if rect[2] <= 0 or rect[3] <= 0:
                raise ValueError(f"Element {eid} has no clickable bounding box: {rect}")
            _ = mouse_click(
                cx,
                cy,
                button="right" if act == "right_click" else "left",
                clicks=2 if act == "double_click" else 1,
            )
            return {"ok": True, "element_id": eid, "action": act, "clicked": [cx, cy]}
        if act == "focus":
            snap.control.SetFocus()
            return {"ok": True, "element_id": eid, "action": "focus"}
        return self._act_pattern(snap, act, value, cx, cy)


def _prune_old_shots(shots_dir: Path) -> None:
    with suppress(OSError):
        if shots_dir.is_dir():
            files = sorted(
                shots_dir.glob("shot_*.*"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for old in files[MAX_SAVED_SHOTS:]:
                old.unlink(missing_ok=True)


def _encode_image(img: Image.Image, *, fmt: str, quality: int) -> tuple[bytes, str, str]:
    fmt_norm = fmt.strip().lower()
    q = max(10, min(95, int(quality)))
    buf = io.BytesIO()
    if fmt_norm in ("jpg", "jpeg"):
        img.save(buf, format="JPEG", quality=q, optimize=True)
        return buf.getvalue(), "image/jpeg", "jpg"
    if fmt_norm == "png":
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png", "png"
    img.save(buf, format="WEBP", quality=q, method=4)
    return buf.getvalue(), "image/webp", "webp"


def _save_shot_if_requested(
    save_path: str, img_bytes: bytes, *, shots_dir: Path, ext: str, now: Callable[[], datetime]
) -> str | None:
    cleaned = save_path.strip()
    if not cleaned:
        return None
    if cleaned.lower() == "auto":
        shots_dir.mkdir(parents=True, exist_ok=True)
        ts = now().astimezone(UTC).strftime("%Y%m%dT%H%M%S_%f")
        dst = shots_dir / f"shot_{ts}.{ext}"
        _ = dst.write_bytes(img_bytes)
        _prune_old_shots(shots_dir)
        return str(dst)
    dst = Path(cleaned)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _ = dst.write_bytes(img_bytes)
    return str(dst)


def capture_screenshot(
    *,
    shots_dir: Path,
    now: Callable[[], datetime],
    x: int = 0,
    y: int = 0,
    width: int = 0,
    height: int = 0,
    hwnd: int = 0,
    title_contains: str = "",
    monitor: int = 1,
    fmt: str = "webp",
    quality: int = 75,
    max_width: int = 1440,
    save_path: str = "",
) -> tuple[dict[str, object], bytes, str]:
    if not is_windows():
        raise RuntimeError("capture_screenshot is only supported on Windows")
    import mss

    target_win: dict[str, object] | None = None
    with mss.mss() as sct:
        if width > 0 and height > 0:
            region = {"left": int(x), "top": int(y), "width": int(width), "height": int(height)}
        elif hwnd > 0 or title_contains.strip():
            target_win = resolve_window(hwnd=hwnd, title_contains=title_contains)
            rect = target_win["rect"]
            if not is_object_seq(rect) or len(rect) != 4:
                raise ValueError(f"Window {target_win['title']!r} has invalid rect {rect}")
            rx, ry, rw, rh = as_int(rect[0]), as_int(rect[1]), as_int(rect[2]), as_int(rect[3])
            if rw <= 0 or rh <= 0:
                raise ValueError(f"Window {target_win['title']!r} has empty rect {rect}")
            region = {"left": rx, "top": ry, "width": rw, "height": rh}
        else:
            idx = max(0, min(len(sct.monitors) - 1, int(monitor)))
            mon = sct.monitors[idx]
            region = {
                "left": int(mon["left"]),
                "top": int(mon["top"]),
                "width": int(mon["width"]),
                "height": int(mon["height"]),
            }
        raw = sct.grab(region)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    orig_w, orig_h = img.size
    max_w = max(0, int(max_width))
    if max_w > 0 and orig_w > max_w:
        img = img.resize(
            (max_w, max(1, round(orig_h * (max_w / orig_w)))), Image.Resampling.LANCZOS
        )
    out_w, out_h = img.size
    img_bytes, mime, ext = _encode_image(img, fmt=fmt, quality=quality)
    saved = _save_shot_if_requested(save_path, img_bytes, shots_dir=shots_dir, ext=ext, now=now)
    meta: dict[str, object] = {
        "ok": True,
        "capture_rect": [region["left"], region["top"], region["width"], region["height"]],
        "image_size": [out_w, out_h],
        "scale_x": round(orig_w / max(1, out_w), 4),
        "scale_y": round(orig_h / max(1, out_h), 4),
        "mime_type": mime,
        "bytes": len(img_bytes),
    }
    if target_win:
        meta["window"] = {"hwnd": target_win["hwnd"], "title": target_win["title"]}
    if saved:
        meta["saved_path"] = saved
    return meta, img_bytes, mime
