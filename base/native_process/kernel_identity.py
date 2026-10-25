from __future__ import annotations

import ctypes
import ctypes.util
from pathlib import Path
from uuid import UUID

from base.native_process.os_platform import is_linux, is_macos


def pid_starttime_ticks(pid: int) -> int | None:
    # meiva:note
    # Linux `/proc` start time in clock ticks since boot, or None when unavailable.
    #
    # The command name in field 2 may include spaces or parentheses, so split the
    # stat record only after its final closing parenthesis.
    #
    try:
        line = Path(f"/proc/{pid}/stat").read_text()
        rest = line.rsplit(")", 1)[1].split()
        ticks = int(rest[22 - 3])
        return ticks if ticks > 0 else None
    except (IndexError, OSError, ValueError):
        return None


def _darwin_boot_session_uuid() -> str:
    # meiva:note
    # The `kern.bootsessionuuid` sysctl, read in-process (a microsecond call, not a spawn).
    name = b"kern.bootsessionuuid"
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    size = ctypes.c_size_t(0)
    if libc.sysctlbyname(name, None, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl {name.decode()} failed")
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctlbyname(name, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl {name.decode()} failed")
    return buffer.value.decode()


def native_boot_id() -> str | None:
    # meiva:note
    # Bind durable POSIX process custody to one native boot, without Settings.
    #
    # A running observer cannot survive reboot, so its boot scope is immutable.
    # Missing or malformed POSIX evidence refuses; it is never a legacy default.
    # Read afresh on every call: both sources are cheap, so no process holds a copy.
    #
    if is_linux():
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    elif is_macos():
        value = _darwin_boot_session_uuid()
    else:
        raise RuntimeError("unsupported native boot identity platform")
    return str(UUID(value))
