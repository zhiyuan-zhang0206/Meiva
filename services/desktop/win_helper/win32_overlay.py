from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

from .constants import CORNER_FAILSAFE_PX, OVERLAY_BORDER_PX, OVERLAY_COLOR_HEX, OVERLAY_COLOR_RGB
from .failsafe_logic import CornerFailsafeState, is_in_corner
from .overlay_render import build_dashed_border_bgra
from .win32_ffi import (
    ATOM,
    BOOL,
    DWORD,
    HANDLE,
    HBITMAP,
    HBRUSH,
    HDC,
    HGDIOBJ,
    HICON,
    HINSTANCE,
    HMENU,
    HWND,
    LONG,
    LPARAM,
    LPCWSTR,
    LPVOID,
    LRESULT,
    MSG,
    POINT,
    UINT,
    ULONG_PTR,
    WORD,
    WPARAM,
    as_int,
    is_windows,
    make_hookproc,
    make_wndproc,
    object_of,
    win_dll,
)

log = logging.getLogger("meiva_win_helper.overlay")

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_NCHITTEST = 0x0084
WM_TIMER = 0x0113
WM_APP_SYNC = 0x8001
WM_APP_CAPTURE_HIDE = 0x8002
WM_APP_CAPTURE_RESTORE = 0x8003
HTTRANSPARENT = -1
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
BI_RGB = 0
SM_CXSCREEN = 0
SM_CYSCREEN = 1
WH_MOUSE_LL = 14


class _BmiHeader(ctypes.Structure):
    bi_size: int
    bi_width: int
    bi_height: int
    bi_planes: int
    bi_bit_count: int
    bi_compression: int
    _fields_ = [
        ("bi_size", DWORD),
        ("bi_width", LONG),
        ("bi_height", LONG),
        ("bi_planes", WORD),
        ("bi_bit_count", WORD),
        ("bi_compression", DWORD),
        ("bi_size_image", DWORD),
        ("bi_x_pels_per_meter", LONG),
        ("bi_y_pels_per_meter", LONG),
        ("bi_clr_used", DWORD),
        ("bi_clr_important", DWORD),
    ]


class _Bmi(ctypes.Structure):
    bmi_header: _BmiHeader
    _fields_ = [("bmi_header", _BmiHeader), ("bmi_colors", DWORD * 3)]


class _Blend(ctypes.Structure):
    _fields_ = [
        ("blend_op", ctypes.c_ubyte),
        ("blend_flags", ctypes.c_ubyte),
        ("source_constant_alpha", ctypes.c_ubyte),
        ("alpha_format", ctypes.c_ubyte),
    ]


class _Size(ctypes.Structure):
    _fields_ = [("cx", LONG), ("cy", LONG)]


class _MsllHook(ctypes.Structure):
    pt: POINT
    dw_extra_info: int
    _fields_ = [
        ("pt", POINT),
        ("mouse_data", DWORD),
        ("flags", DWORD),
        ("time", DWORD),
        ("dw_extra_info", ULONG_PTR),
    ]


class _WndClassExW(ctypes.Structure):
    _fields_ = [
        ("cbSize", UINT),
        ("style", UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HICON),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
        ("hIconSm", HICON),
    ]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def init_dpi_awareness() -> str:
    if not is_windows():
        return "non_windows"
    try:
        _ = object_of(win_dll("shcore").SetProcessDpiAwareness, 2)
        return "per_monitor_v1"
    except (OSError, AttributeError, RuntimeError):
        try:
            _ = object_of(win_dll("user32").SetProcessDPIAware)
            return "system_dpi_aware"
        except (OSError, AttributeError, RuntimeError):
            return "unaware"


def get_session_info(*, pid: int) -> dict[str, object]:
    if not is_windows():
        return {
            "pid": pid,
            "session_id": -1,
            "desktop_name": "non_windows",
            "desktop_interactive": False,
        }
    kernel32, user32 = win_dll("kernel32"), win_dll("user32")
    session_id = DWORD(0)
    _ = object_of(kernel32.ProcessIdToSessionId, pid, ctypes.byref(session_id))
    desktop_name = ""
    hdesk = object_of(user32.OpenInputDesktop, 0, 0, 0x0001)
    if hdesk:
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = DWORD(0)
            if object_of(
                user32.GetUserObjectInformationW,
                hdesk,
                2,
                buf,
                ctypes.sizeof(buf),
                ctypes.byref(needed),
            ):
                desktop_name = ctypes.wstring_at(buf)
        finally:
            _ = object_of(user32.CloseDesktop, hdesk)
    return {
        "pid": pid,
        "session_id": int(session_id.value),
        "desktop_name": desktop_name or "unknown",
        "desktop_interactive": desktop_name.lower() == "default",
    }


def get_cursor_pos() -> tuple[int, int] | None:
    if not is_windows():
        return None
    pt = POINT()
    if object_of(win_dll("user32").GetCursorPos, ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return None


def _configure_paint_dlls(user32: ctypes.CDLL, gdi32: ctypes.CDLL) -> None:
    user32.GetDC.restype = HDC
    gdi32.CreateCompatibleDC.argtypes = [HDC]
    gdi32.CreateCompatibleDC.restype = HDC
    gdi32.CreateDIBSection.argtypes = [
        HDC,
        ctypes.POINTER(_Bmi),
        UINT,
        ctypes.POINTER(ctypes.c_void_p),
        HANDLE,
        DWORD,
    ]
    gdi32.CreateDIBSection.restype = HBITMAP
    gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
    gdi32.SelectObject.restype = HGDIOBJ
    gdi32.DeleteObject.argtypes = [HGDIOBJ]
    gdi32.DeleteDC.argtypes = [HDC]
    user32.ReleaseDC.argtypes = [HWND, HDC]
    user32.UpdateLayeredWindow.argtypes = [
        HWND,
        HDC,
        ctypes.POINTER(POINT),
        ctypes.POINTER(_Size),
        HDC,
        ctypes.POINTER(POINT),
        DWORD,
        ctypes.POINTER(_Blend),
        DWORD,
    ]
    user32.UpdateLayeredWindow.restype = BOOL


def _configure_window_dlls(user32: ctypes.CDLL, kernel32: ctypes.CDLL) -> None:
    user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    kernel32.GetModuleHandleW.argtypes = [LPCWSTR]
    kernel32.GetModuleHandleW.restype = HINSTANCE
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WndClassExW)]
    user32.RegisterClassExW.restype = ATOM
    user32.CreateWindowExW.argtypes = [
        DWORD,
        LPCWSTR,
        LPCWSTR,
        DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        HWND,
        HMENU,
        HINSTANCE,
        LPVOID,
    ]
    user32.CreateWindowExW.restype = HWND


class BorderOverlay:
    def __init__(self, should_show_fn: Callable[[], bool]) -> None:
        self._should_show_fn = should_show_fn
        self._lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._hwnd = 0
        self._thread_id = 0
        self._executor: ThreadPoolExecutor | None = None
        self._loop_future: Future[None] | None = None
        self._ready = threading.Event()
        self._wndproc_ref: object = None
        self._desired_visible = False
        self._actually_visible = False
        self._hidden_for_capture = False
        self._capture_ack = threading.Event()
        self._size: tuple[int, int] = (0, 0)
        self._error = ""

    @property
    def hwnd(self) -> int:
        return self._hwnd

    def start(self) -> None:
        if not is_windows() or (self._loop_future is not None and not self._loop_future.done()):
            return
        self._ready.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="meiva-border-overlay"
        )
        self._loop_future = self._executor.submit(self._run_ui_loop)
        _ = self._ready.wait(timeout=3.0)

    def stop(self) -> None:
        if is_windows() and self._thread_id:
            with suppress(OSError, RuntimeError):
                _ = object_of(win_dll("user32").PostThreadMessageW, self._thread_id, WM_QUIT, 0, 0)
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
            self._loop_future = None

    def sync_visibility(self, *, visible: bool | None = None) -> None:
        with self._lock:
            self._desired_visible = (
                bool(self._should_show_fn()) if visible is None else bool(visible)
            )
            hwnd = self._hwnd
        if is_windows() and hwnd:
            _ = object_of(win_dll("user32").PostMessageW, HWND(hwnd), WM_APP_SYNC, 0, 0)

    @contextmanager
    def hide_for_capture(self) -> Generator[None]:
        if not is_windows():
            yield
            return
        user32 = win_dll("user32")
        with self._capture_lock:
            with self._lock:
                was_visible, hwnd = self._actually_visible, self._hwnd
                if was_visible and hwnd:
                    self._hidden_for_capture = True
                    self._capture_ack.clear()
            if was_visible and hwnd:
                _ = object_of(user32.PostMessageW, HWND(hwnd), WM_APP_CAPTURE_HIDE, 0, 0)
                _ = self._capture_ack.wait(timeout=0.5)
                self._flush_dwm()
            try:
                yield
            finally:
                if was_visible and hwnd:
                    with self._lock:
                        self._hidden_for_capture = False
                        self._capture_ack.clear()
                    _ = object_of(user32.PostMessageW, HWND(hwnd), WM_APP_CAPTURE_RESTORE, 0, 0)
                    _ = self._capture_ack.wait(timeout=0.5)

    def _flush_dwm(self) -> None:
        if not is_windows():
            return
        try:
            dwm = win_dll("dwmapi")
            for _ in range(2):
                if as_int(object_of(dwm.DwmFlush), 1) != 0:
                    time.sleep(0.03)
                    break
        except (OSError, AttributeError, RuntimeError):
            time.sleep(0.04)

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "ready": bool(self._hwnd),
                "hwnd": self._hwnd,
                "visible": self._actually_visible,
                "desired_visible": self._desired_visible,
                "color_hex": OVERLAY_COLOR_HEX,
                "border_px": OVERLAY_BORDER_PX,
                "size": list(self._size),
                "error": self._error or None,
            }

    def _apply_show(self, hwnd: int, *, show: bool) -> None:
        user32 = win_dll("user32")
        if show:
            _ = object_of(
                user32.SetWindowPos,
                HWND(hwnd),
                HWND(-1),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            with self._lock:
                self._actually_visible = True
        else:
            _ = object_of(user32.ShowWindow, HWND(hwnd), SW_HIDE)
            with self._lock:
                self._actually_visible = False

    def _paint_layered(self, hwnd: int, width: int, height: int) -> None:
        user32, gdi32 = win_dll("user32"), win_dll("gdi32")
        _configure_paint_dlls(user32, gdi32)
        bgra = build_dashed_border_bgra(width, height)
        bmi = _Bmi()
        bmi.bmi_header.bi_size = ctypes.sizeof(_BmiHeader)
        bmi.bmi_header.bi_width, bmi.bmi_header.bi_height = width, -height
        bmi.bmi_header.bi_planes, bmi.bmi_header.bi_bit_count, bmi.bmi_header.bi_compression = (
            1,
            32,
            BI_RGB,
        )
        screen_dc = object_of(user32.GetDC, None)
        mem_dc = object_of(gdi32.CreateCompatibleDC, screen_dc)
        bits_ptr = ctypes.c_void_p()
        hbmp = object_of(
            gdi32.CreateDIBSection,
            screen_dc,
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
            ctypes.byref(bits_ptr),
            None,
            0,
        )
        if not hbmp or not bits_ptr.value:
            _ = object_of(gdi32.DeleteDC, mem_dc)
            _ = object_of(user32.ReleaseDC, None, screen_dc)
            raise RuntimeError("CreateDIBSection failed for border overlay")
        old_bmp = object_of(gdi32.SelectObject, mem_dc, hbmp)
        try:
            _ = ctypes.memmove(bits_ptr.value, bgra, len(bgra))
            pt_dst, sz, pt_src = POINT(0, 0), _Size(width, height), POINT(0, 0)
            blend = _Blend(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = object_of(
                user32.UpdateLayeredWindow,
                HWND(hwnd),
                screen_dc,
                ctypes.byref(pt_dst),
                ctypes.byref(sz),
                mem_dc,
                ctypes.byref(pt_src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
            if not ok:
                raise RuntimeError("UpdateLayeredWindow failed")
        finally:
            _ = object_of(gdi32.SelectObject, mem_dc, old_bmp)
            _ = object_of(gdi32.DeleteObject, hbmp)
            _ = object_of(gdi32.DeleteDC, mem_dc)
            _ = object_of(user32.ReleaseDC, None, screen_dc)

    def _handle_wnd_msg(
        self, user32: ctypes.CDLL, hwnd: int, msg: int, wparam: int, lparam: int
    ) -> int:
        if msg == WM_NCHITTEST:
            return HTTRANSPARENT
        if msg == WM_APP_CAPTURE_HIDE:
            self._apply_show(hwnd, show=False)
            self._capture_ack.set()
            return 0
        if msg == WM_APP_CAPTURE_RESTORE:
            with self._lock:
                should_show = self._desired_visible and not self._hidden_for_capture
            if should_show:
                self._apply_show(hwnd, show=True)
            self._capture_ack.set()
            return 0
        if msg == WM_APP_SYNC:
            with self._lock:
                if self._hidden_for_capture:
                    return 0
                want = self._desired_visible
            self._apply_show(hwnd, show=want)
            return 0
        if msg == WM_TIMER:
            want = bool(self._should_show_fn())
            with self._lock:
                self._desired_visible = want
                if self._hidden_for_capture:
                    return 0
                cur = self._actually_visible
            if want != cur:
                self._apply_show(hwnd, show=want)
            return 0
        if msg == WM_DESTROY:
            _ = object_of(user32.PostQuitMessage, 0)
            return 0
        return as_int(object_of(user32.DefWindowProcW, hwnd, msg, wparam, lparam))

    def _run_ui_loop(self) -> None:
        if not is_windows():
            self._ready.set()
            return
        user32, kernel32 = win_dll("user32"), win_dll("kernel32")
        _configure_window_dlls(user32, kernel32)
        self._thread_id = as_int(object_of(kernel32.GetCurrentThreadId))

        def wndproc(hwnd: int, msg: int, wp: int, lp: int) -> int:
            return self._handle_wnd_msg(user32, hwnd, msg, wp, lp)

        self._wndproc_ref, wndproc_ptr = make_wndproc(wndproc)
        hinst = object_of(kernel32.GetModuleHandleW, None)
        cls_name = f"MeivaBorderOverlay_{self._thread_id}"
        wc = _WndClassExW(
            cbSize=ctypes.sizeof(_WndClassExW),
            lpfnWndProc=wndproc_ptr,
            hInstance=hinst,
            lpszClassName=cls_name,
        )
        if not object_of(user32.RegisterClassExW, ctypes.byref(wc)):
            self._error = "RegisterClassExW failed"
            self._ready.set()
            return
        w = max(1, as_int(object_of(user32.GetSystemMetrics, SM_CXSCREEN)))
        h = max(1, as_int(object_of(user32.GetSystemMetrics, SM_CYSCREEN)))
        ex_style = (
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        )
        hwnd_val = as_int(
            object_of(
                user32.CreateWindowExW,
                ex_style,
                cls_name,
                "",
                WS_POPUP,
                0,
                0,
                w,
                h,
                None,
                None,
                hinst,
                None,
            )
        )
        if not hwnd_val:
            self._error = "CreateWindowExW failed"
            self._ready.set()
            return
        try:
            self._paint_layered(hwnd_val, w, h)
        except Exception as exc:
            self._error = f"paint_layered failed: {exc}"
            _ = object_of(user32.DestroyWindow, HWND(hwnd_val))
            self._ready.set()
            return
        with self._lock:
            self._hwnd, self._size = hwnd_val, (w, h)
            self._desired_visible = bool(self._should_show_fn())
        if self._desired_visible:
            self._apply_show(hwnd_val, show=True)
        _ = object_of(user32.SetTimer, HWND(hwnd_val), 1, 1000, None)
        log.info(
            "Sunset Orange (#%02X%02X%02X) dashed border overlay ready (%dx%d)",
            *OVERLAY_COLOR_RGB,
            w,
            h,
        )
        self._ready.set()
        self._pump_messages(user32, hwnd_val)

    def _pump_messages(self, user32: ctypes.CDLL, hwnd_val: int) -> None:
        msg = MSG()
        try:
            while as_int(object_of(user32.GetMessageW, ctypes.byref(msg), None, 0, 0)) > 0:
                _ = object_of(user32.TranslateMessage, ctypes.byref(msg))
                _ = object_of(user32.DispatchMessageW, ctypes.byref(msg))
        finally:
            _ = object_of(user32.KillTimer, HWND(hwnd_val), 1)
            _ = object_of(user32.DestroyWindow, HWND(hwnd_val))
            with self._lock:
                self._hwnd = 0
                self._actually_visible = False


class CornerFailsafeGuard:
    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        on_abort: Callable[[tuple[int, int]], str | None] | None = None,
        has_active_session: Callable[[], bool] | None = None,
        threshold_px: int = CORNER_FAILSAFE_PX,
    ) -> None:
        self._lock = threading.Lock()
        self._now = now
        self._on_abort = on_abort
        self._has_active_session = has_active_session
        self._state = CornerFailsafeState(threshold_px=threshold_px)
        self._hook_active = False
        self._hook_error = ""
        self._thread_id = 0
        self._hook_executor: ThreadPoolExecutor | None = None
        self._hook_future: Future[None] | None = None
        self._abort_executor: ThreadPoolExecutor | None = None
        self._ready = threading.Event()
        self._mouse_cb: object = None

    def start(self) -> None:
        if not is_windows() or (self._hook_future is not None and not self._hook_future.done()):
            return
        self._ready.clear()
        self._abort_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="corner-failsafe-abort"
        )
        self._hook_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="corner-failsafe-guard"
        )
        self._hook_future = self._hook_executor.submit(self._run_hook_loop)
        _ = self._ready.wait(timeout=3.0)

    def stop(self) -> None:
        if is_windows() and self._thread_id:
            with suppress(OSError, RuntimeError):
                _ = object_of(win_dll("user32").PostThreadMessageW, self._thread_id, WM_QUIT, 0, 0)
        if self._hook_executor is not None:
            self._hook_executor.shutdown(wait=True, cancel_futures=True)
            self._hook_executor = None
            self._hook_future = None
        if self._abort_executor is not None:
            self._abort_executor.shutdown(wait=False, cancel_futures=True)
            self._abort_executor = None

    def trigger_abort(self, pos: tuple[int, int]) -> None:
        prev_holder: str | None = None
        if self._on_abort is not None:
            try:
                prev_holder = self._on_abort(pos)
            except Exception:
                log.warning("corner failsafe on_abort callback failed", exc_info=True)
        with self._lock:
            self._state, transitioned = self._state.with_abort(
                pos, now_iso=_iso(self._now()), previous_holder=prev_holder
            )
        if transitioned:
            log.warning(
                "CORNER FAILSAFE TRIGGERED at (%d, %d); holder=%s", pos[0], pos[1], prev_holder
            )

    def clear_for_new_session(self) -> dict[str, object] | None:
        pos = get_cursor_pos()
        with self._lock:
            self._state, err = self._state.cleared_for_new_session(pos)
            return err

    def check_allowed(self) -> dict[str, object] | None:
        pos = get_cursor_pos()
        if pos is not None and is_in_corner(pos[0], pos[1], self._state.threshold_px):
            self.trigger_abort(pos)
            with self._lock:
                return self._state.abort_error(pos=pos)
        with self._lock:
            if self._state.aborted:
                return self._state.abort_error()
        return None

    def status(self) -> dict[str, object]:
        pos = get_cursor_pos()
        with self._lock:
            st = self._state
            return {
                "mode": "corner_failsafe",
                "hook_active": self._hook_active,
                "hook_error": self._hook_error or None,
                "corner_threshold_px": st.threshold_px,
                "cursor_position": list(pos) if pos is not None else None,
                "cursor_in_corner_now": pos is not None
                and is_in_corner(pos[0], pos[1], st.threshold_px),
                "session_aborted": st.aborted,
                "aborted_at": st.aborted_at,
                "aborted_position": list(st.aborted_position)
                if st.aborted_position is not None
                else None,
                "aborted_previous_holder": st.aborted_previous_holder,
                "abort_count": st.abort_count,
            }

    def _run_hook_loop(self) -> None:
        if not is_windows():
            self._ready.set()
            return
        user32, kernel32 = win_dll("user32"), win_dll("kernel32")
        self._thread_id = as_int(object_of(kernel32.GetCurrentThreadId))
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, HINSTANCE, DWORD]
        user32.SetWindowsHookExW.restype = HANDLE
        user32.CallNextHookEx.argtypes = [HANDLE, ctypes.c_int, WPARAM, LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [HANDLE]
        user32.UnhookWindowsHookEx.restype = BOOL

        def mouse_proc(n_code: int, w_param: int, l_param: int) -> int:
            if n_code >= 0 and l_param:
                try:
                    info = ctypes.cast(l_param, ctypes.POINTER(_MsllHook)).contents
                    has_session = self._has_active_session is None or self._has_active_session()
                    with self._lock:
                        trigger = self._state.should_trigger_from_hook(
                            int(info.pt.x),
                            int(info.pt.y),
                            extra_info=int(info.dw_extra_info),
                            has_active_session=has_session,
                        )
                    if trigger and self._abort_executor is not None:
                        _ = self._abort_executor.submit(
                            self.trigger_abort, (int(info.pt.x), int(info.pt.y))
                        )
                except Exception:
                    log.warning("corner failsafe mouse_proc failed", exc_info=True)
            return as_int(object_of(user32.CallNextHookEx, None, n_code, w_param, l_param))

        self._mouse_cb, mouse_ptr = make_hookproc(mouse_proc)
        h_mouse = object_of(user32.SetWindowsHookExW, WH_MOUSE_LL, mouse_ptr, None, 0)
        if not h_mouse:
            self._hook_error = "SetWindowsHookExW(WH_MOUSE_LL) failed"
            self._ready.set()
            return
        self._hook_active = True
        self._ready.set()
        msg = MSG()
        try:
            while as_int(object_of(user32.GetMessageW, ctypes.byref(msg), None, 0, 0)) > 0:
                _ = object_of(user32.TranslateMessage, ctypes.byref(msg))
                _ = object_of(user32.DispatchMessageW, ctypes.byref(msg))
        finally:
            _ = object_of(user32.UnhookWindowsHookEx, h_mouse)
            self._hook_active = False
