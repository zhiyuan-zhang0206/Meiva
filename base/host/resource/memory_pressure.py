# meiva:note
# The operating system's own memory-pressure state and per-process footprint.
#
# Two reads, both stateless: `pressure()` is the level the kernel itself reports
# (no threshold of ours is involved), `footprint(pid)` is the memory the kernel
# charges to one process. macOS only today: `kern.memorystatus_vm_pressure_level`
# (1 normal, 2 warning, 4 critical) and `proc_pid_rusage`'s `ri_phys_footprint`,
# which, unlike the RSS `ps` shows, counts compressed and swapped pages. Linux
# has no equivalent state-typed signal (PSI and cgroup events are rates and
# counters), so `host_memory_source()` returns None there and nothing acts on it.

from __future__ import annotations

import ctypes
import errno
import struct
from typing import Literal, Protocol

from base.native_process.os_platform import is_macos

PressureLevel = Literal["normal", "warning", "critical"]

_DARWIN_LEVELS: dict[int, PressureLevel] = {1: "normal", 2: "warning", 4: "critical"}
_RUSAGE_INFO_V2 = 2
# meiva:note
# `struct rusage_info_v2`: a 16-byte uuid, then u64 counters; `ri_phys_footprint` is the 8th of them.
_PHYS_FOOTPRINT_OFFSET = 16 + 7 * 8
_RUSAGE_BUFFER_BYTES = 512


# meiva:note
# What the memory guard reads; tests substitute a fake.
class MemorySource(Protocol):
    def pressure(self) -> PressureLevel: ...

    # meiva:note
    # Bytes the kernel charges to `pid`; 0 when the process is gone.
    def footprint(self, pid: int) -> int: ...


# meiva:note
# These probes need Darwin's system ABI, not a library selected through the
# launching process's search path. dyld resolves these absolute install names
# from the shared cache even when they have no standalone file on disk.
class _DarwinMemory:
    def pressure(self) -> PressureLevel:
        libc = ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        name = b"kern.memorystatus_vm_pressure_level"
        if libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), f"sysctl {name.decode()} failed")
        return _DARWIN_LEVELS[value.value]

    def footprint(self, pid: int) -> int:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buffer = ctypes.create_string_buffer(_RUSAGE_BUFFER_BYTES)
        if libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V2, buffer) != 0:
            if ctypes.get_errno() == errno.ESRCH:
                return 0
            raise OSError(ctypes.get_errno(), f"proc_pid_rusage({pid}) failed")
        return struct.unpack_from("<Q", buffer, _PHYS_FOOTPRINT_OFFSET)[0]


# meiva:note
# The platform's memory source, or None where the OS exposes no pressure state.
def host_memory_source() -> MemorySource | None:
    return _DarwinMemory() if is_macos() else None
