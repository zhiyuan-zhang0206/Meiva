"""Linux process descriptors through libc, independent of Python build headers.

Managed Python 3.12 builds may omit os.pidfd_open and signal.pidfd_send_signal.
Use one native implementation for every runtime; missing libc/kernel support
fails before custody is granted, never via kill(pid).
"""

from __future__ import annotations

import ctypes
import os
import sys
from functools import cache


def require_pidfd_support(*, platform: str, has_symbols: bool) -> None:
    if platform != "linux":
        raise RuntimeError("process descriptors require Linux")
    if not has_symbols:
        raise RuntimeError("Linux process custody requires libc pidfd support")


@cache
def pidfd_libc() -> ctypes.CDLL:
    platform = sys.platform
    require_pidfd_support(platform=platform, has_symbols=True)
    libc = ctypes.CDLL(None, use_errno=True)
    require_pidfd_support(
        platform=platform,
        has_symbols=hasattr(libc, "pidfd_open") and hasattr(libc, "pidfd_send_signal"),
    )
    opened = libc.pidfd_open
    sent = libc.pidfd_send_signal
    opened.argtypes = [ctypes.c_int, ctypes.c_uint]
    opened.restype = ctypes.c_int
    sent.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    sent.restype = ctypes.c_int
    return libc


def open_process(pid: int, *, simulated_errno: int | None = None) -> int:
    """Return a non-inheritable descriptor; the caller owns its close."""
    if simulated_errno is not None:
        raise OSError(simulated_errno, f"pidfd_open({pid}): {os.strerror(simulated_errno)}")
    descriptor = pidfd_libc().pidfd_open(pid, 0)
    if descriptor < 0:
        code = ctypes.get_errno()
        raise OSError(code, f"pidfd_open({pid}): {os.strerror(code)}")
    return descriptor


def send_signal(descriptor: int, signum: int, *, simulated_errno: int | None = None) -> None:
    """Signal the retained task, preserving native errno for the custody owner."""
    if simulated_errno is not None:
        raise OSError(
            simulated_errno, f"pidfd_send_signal({descriptor}): {os.strerror(simulated_errno)}"
        )
    if pidfd_libc().pidfd_send_signal(descriptor, signum, None, 0) < 0:
        code = ctypes.get_errno()
        raise OSError(code, f"pidfd_send_signal({descriptor}): {os.strerror(code)}")
