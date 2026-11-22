from __future__ import annotations

import ctypes
import time

from .constants import MEIVA_INPUT_TAG
from .input_logic import (
    EXTENDED_VKS,
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYBDINPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    MOUSEINPUT,
    WHEEL_DELTA,
    button_flags,
    normalize_virtual_coords,
    resolve_vk_pure,
)
from .win32_ffi import HANDLE, HGLOBAL, UINT, as_int, is_windows, object_of, win_dll

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _send_inputs(inputs: list[INPUT]) -> int:
    if not is_windows():
        raise RuntimeError("SendInput is only supported on Windows")
    if not inputs:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    user32 = win_dll("user32")
    return as_int(object_of(user32.SendInput, len(inputs), arr, ctypes.sizeof(INPUT)))


def _to_abs_virtual(x: int, y: int) -> tuple[int, int]:
    user32 = win_dll("user32")
    return normalize_virtual_coords(
        x,
        y,
        vx=as_int(object_of(user32.GetSystemMetrics, SM_XVIRTUALSCREEN)),
        vy=as_int(object_of(user32.GetSystemMetrics, SM_YVIRTUALSCREEN)),
        vw=max(1, as_int(object_of(user32.GetSystemMetrics, SM_CXVIRTUALSCREEN))),
        vh=max(1, as_int(object_of(user32.GetSystemMetrics, SM_CYVIRTUALSCREEN))),
    )


def _mouse_event(dx: int, dy: int, flags: int, data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(
            dx=dx,
            dy=dy,
            mouseData=ctypes.c_uint32(data & 0xFFFFFFFF).value,
            dwFlags=flags,
            time=0,
            dwExtraInfo=MEIVA_INPUT_TAG,
        ),
    )


def _vk_event(vk: int, *, key_up: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if key_up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=MEIVA_INPUT_TAG),
    )


def _resolve_vk(token: str) -> int:
    pure = resolve_vk_pure(token)
    if pure is not None:
        return pure
    t = token.strip().lower()
    if is_windows() and len(t) == 1:
        res = as_int(object_of(win_dll("user32").VkKeyScanW, ord(t)), -1)
        if res != -1:
            return res & 0xFF
    raise ValueError(f"Unknown key token: {token!r}")


def mouse_move(x: int, y: int) -> None:
    nx, ny = _to_abs_virtual(x, y)
    _ = _send_inputs(
        [_mouse_event(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)]
    )


def mouse_click(
    x: int, y: int, *, button: str = "left", clicks: int = 1, modifiers: str = ""
) -> dict[str, object]:
    down_flag, up_flag = button_flags(button)
    mod_vks = [_resolve_vk(m) for m in modifiers.replace("+", ",").split(",") if m.strip()]
    nx, ny = _to_abs_virtual(x, y)
    move_flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    if mod_vks:
        _ = _send_inputs([_vk_event(vk, key_up=False) for vk in mod_vks])
    try:
        for i in range(max(1, min(3, int(clicks)))):
            _ = _send_inputs(
                [
                    _mouse_event(nx, ny, move_flags),
                    _mouse_event(
                        nx, ny, down_flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
                    ),
                    _mouse_event(nx, ny, up_flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK),
                ]
            )
            if i + 1 < clicks:
                time.sleep(0.05)
    finally:
        if mod_vks:
            _ = _send_inputs([_vk_event(vk, key_up=True) for vk in reversed(mod_vks)])
    return {"ok": True, "clicked": [x, y], "button": button, "clicks": clicks}


def mouse_move_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    *,
    button: str = "left",
    duration_ms: int = 250,
    steps: int = 12,
) -> dict[str, object]:
    down_flag, up_flag = button_flags(button)
    steps = max(2, min(60, int(steps)))
    delay = max(0.005, min(2.0, duration_ms / 1000.0)) / steps
    sx, sy = _to_abs_virtual(start_x, start_y)
    _ = _send_inputs(
        [
            _mouse_event(sx, sy, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK),
            _mouse_event(sx, sy, down_flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK),
        ]
    )
    try:
        for i in range(1, steps + 1):
            time.sleep(delay)
            cx = round(start_x + (end_x - start_x) * (i / steps))
            cy = round(start_y + (end_y - start_y) * (i / steps))
            nx, ny = _to_abs_virtual(cx, cy)
            _ = _send_inputs(
                [
                    _mouse_event(
                        nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
                    )
                ]
            )
    finally:
        ex, ey = _to_abs_virtual(end_x, end_y)
        _ = _send_inputs(
            [_mouse_event(ex, ey, up_flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)]
        )
    return {"ok": True, "start": [start_x, start_y], "end": [end_x, end_y], "button": button}


def mouse_scroll(x: int, y: int, dy: int, *, dx: int = 0) -> dict[str, object]:
    mouse_move(x, y)
    events: list[INPUT] = []
    if dy:
        events.append(_mouse_event(0, 0, MOUSEEVENTF_WHEEL, int(dy) * WHEEL_DELTA))
    if dx:
        events.append(_mouse_event(0, 0, MOUSEEVENTF_HWHEEL, int(dx) * WHEEL_DELTA))
    _ = _send_inputs(events)
    return {"ok": True, "at": [x, y], "dy": dy, "dx": dx}


def press_key(combo: str) -> dict[str, object]:
    tokens = [t.strip() for t in combo.split("+") if t.strip()]
    if not tokens:
        raise ValueError("keys combo cannot be empty")
    vks = [_resolve_vk(t) for t in tokens]
    events = [_vk_event(vk, key_up=False) for vk in vks] + [
        _vk_event(vk, key_up=True) for vk in reversed(vks)
    ]
    _ = _send_inputs(events)
    return {"ok": True, "keys": combo}


def type_text(text: str, *, enter: bool = False, clear_first: bool = False) -> dict[str, object]:
    if clear_first:
        _ = press_key("Ctrl+A")
        time.sleep(0.03)
        _ = press_key("Backspace")
        time.sleep(0.03)
    events: list[INPUT] = []
    utf16 = text.encode("utf-16-le")
    for i in range(0, len(utf16), 2):
        code_unit = int.from_bytes(utf16[i : i + 2], "little")
        if code_unit == 0x000A:
            events.append(_vk_event(0x0D, key_up=False))
            events.append(_vk_event(0x0D, key_up=True))
        elif code_unit != 0x000D:
            events.append(
                INPUT(
                    type=INPUT_KEYBOARD,
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=code_unit,
                        dwFlags=KEYEVENTF_UNICODE,
                        time=0,
                        dwExtraInfo=MEIVA_INPUT_TAG,
                    ),
                )
            )
            events.append(
                INPUT(
                    type=INPUT_KEYBOARD,
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=code_unit,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=MEIVA_INPUT_TAG,
                    ),
                )
            )
    if enter:
        events.append(_vk_event(0x0D, key_up=False))
        events.append(_vk_event(0x0D, key_up=True))
    for start in range(0, len(events), 64):
        _ = _send_inputs(events[start : start + 64])
        if start + 64 < len(events):
            time.sleep(0.01)
    return {"ok": True, "chars": len(text), "enter": enter}


def clipboard_get() -> dict[str, object]:
    if not is_windows():
        return {"ok": False, "error": "Clipboard access is only supported on Windows"}
    user32, kernel32 = win_dll("user32"), win_dll("kernel32")
    user32.GetClipboardData.restype = HANDLE
    kernel32.GlobalLock.argtypes = [HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [HGLOBAL]
    if not object_of(user32.OpenClipboard, None):
        return {"ok": False, "error": "Could not open clipboard"}
    try:
        if not object_of(user32.IsClipboardFormatAvailable, CF_UNICODETEXT):
            return {"ok": True, "text": None, "length": 0}
        handle = object_of(user32.GetClipboardData, CF_UNICODETEXT)
        if not handle:
            return {"ok": True, "text": None, "length": 0}
        ptr = object_of(kernel32.GlobalLock, handle)
        if not isinstance(ptr, int) or ptr == 0:
            return {"ok": False, "error": "GlobalLock failed"}
        try:
            text = ctypes.wstring_at(ptr)
        finally:
            _ = object_of(kernel32.GlobalUnlock, handle)
        return {
            "ok": True,
            "text": text[:32768],
            "length": len(text),
            "truncated": len(text) > 32768,
        }
    finally:
        _ = object_of(user32.CloseClipboard)


def clipboard_set(text: str) -> dict[str, object]:
    if not is_windows():
        return {"ok": False, "error": "Clipboard access is only supported on Windows"}
    user32, kernel32 = win_dll("user32"), win_dll("kernel32")
    kernel32.GlobalAlloc.argtypes = [UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = HGLOBAL
    kernel32.GlobalLock.argtypes = [HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [HGLOBAL]
    user32.SetClipboardData.argtypes = [UINT, HANDLE]
    user32.SetClipboardData.restype = HANDLE
    data = (text + "\x00").encode("utf-16-le")
    if not object_of(user32.OpenClipboard, None):
        return {"ok": False, "error": "Could not open clipboard"}
    try:
        _ = object_of(user32.EmptyClipboard)
        hmem = object_of(kernel32.GlobalAlloc, GMEM_MOVEABLE, len(data))
        if not hmem:
            return {"ok": False, "error": "GlobalAlloc failed"}
        ptr = object_of(kernel32.GlobalLock, hmem)
        if not isinstance(ptr, int) or ptr == 0:
            _ = object_of(kernel32.GlobalFree, hmem)
            return {"ok": False, "error": "GlobalLock failed"}
        try:
            _ = ctypes.memmove(ptr, data, len(data))
        finally:
            _ = object_of(kernel32.GlobalUnlock, hmem)
        if not object_of(user32.SetClipboardData, CF_UNICODETEXT, hmem):
            _ = object_of(kernel32.GlobalFree, hmem)
            return {"ok": False, "error": "SetClipboardData failed"}
        return {"ok": True, "length": len(text)}
    finally:
        _ = object_of(user32.CloseClipboard)
