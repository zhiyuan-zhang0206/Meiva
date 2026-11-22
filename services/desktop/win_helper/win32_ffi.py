from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from typing import TypeGuard

IS_X64 = ctypes.sizeof(ctypes.c_void_p) == 8
ULONG_PTR = ctypes.c_uint64 if IS_X64 else ctypes.c_uint32
LRESULT = ctypes.c_int64 if IS_X64 else ctypes.c_int32
WPARAM = ULONG_PTR
LPARAM = LRESULT
HWND = ctypes.c_void_p
HDC = ctypes.c_void_p
HBITMAP = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p
HMENU = ctypes.c_void_p
HICON = ctypes.c_void_p
HBRUSH = ctypes.c_void_p
HANDLE = ctypes.c_void_p
HGLOBAL = ctypes.c_void_p
LPVOID = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p
DWORD = ctypes.c_uint32
UINT = ctypes.c_uint32
WORD = ctypes.c_uint16
ATOM = ctypes.c_uint16
LONG = ctypes.c_int32
BOOL = ctypes.c_int32


class POINT(ctypes.Structure):
    x: int
    y: int
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(ctypes.Structure):
    left: int
    top: int
    right: int
    bottom: int
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", HWND),
        ("message", UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
    ]


def is_windows() -> bool:
    return sys.platform == "win32"


def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_dict(value) and all(isinstance(k, str) for k in value)


def is_object_seq(value: object) -> TypeGuard[list[object] | tuple[object, ...]]:
    return isinstance(value, (list, tuple))


def as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def win_dll(name: str) -> ctypes.CDLL:
    if sys.platform != "win32":
        raise RuntimeError(f"Windows DLL {name!r} is only available on win32")
    return ctypes.WinDLL(name)


def make_wndproc(fn: Callable[[int, int, int, int], int]) -> tuple[object, int]:
    if sys.platform != "win32":
        raise RuntimeError("Win32 callbacks are only available on win32")
    cb_type = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)
    cb = cb_type(fn)
    ptr = ctypes.cast(cb, ctypes.c_void_p).value or 0
    return cb, ptr


def make_hookproc(fn: Callable[[int, int, int], int]) -> tuple[object, int]:
    if sys.platform != "win32":
        raise RuntimeError("Win32 callbacks are only available on win32")
    cb_type = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)
    cb = cb_type(fn)
    ptr = ctypes.cast(cb, ctypes.c_void_p).value or 0
    return cb, ptr


def make_enumproc(fn: Callable[[int, int], int]) -> object:
    if sys.platform != "win32":
        raise RuntimeError("Win32 callbacks are only available on win32")
    cb_type = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)
    return cb_type(fn)
