"""Fresh-interpreter probes of ``base.native_process.os_platform``; never imported by a production entry.

Run as ``python [-I] _os_platform_child.py <scenario> [repo-root]``.
"""

from __future__ import annotations

import sys
import time


def cold_import_does_not_probe_uname() -> None:
    import platform  # noqa: F401  # pyright: ignore[reportUnusedImport]
    import uuid  # noqa: F401  # pyright: ignore[reportUnusedImport]

    # Linux's stdlib UUID initialization probes the OS independently of Meiva.
    assert "base.native_process" not in sys.modules
    assert "base.native_process.os_platform" not in sys.modules

    called_uname: list[str] = []

    def profile(frame: object, event: str, _arg: object) -> None:
        if event == "call":
            code = getattr(frame, "f_code", None)
            if getattr(code, "co_name", None) == "uname":
                called_uname.append("uname")

    sys.setprofile(profile)
    try:
        from base.native_process import os_platform
    finally:
        sys.setprofile(None)
    assert os_platform is not None
    assert not called_uname, "platform import must not probe uname"


def pipe_stdout_streams_before_exit() -> None:
    sys.path.insert(0, sys.argv[2])
    from base.native_process.os_platform import ensure_line_buffered_stdio

    ensure_line_buffered_stdio()
    sys.stdout.write("[update] header\n")
    time.sleep(30)


SCENARIOS = {
    "cold_import_does_not_probe_uname": cold_import_does_not_probe_uname,
    "pipe_stdout_streams_before_exit": pipe_stdout_streams_before_exit,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
