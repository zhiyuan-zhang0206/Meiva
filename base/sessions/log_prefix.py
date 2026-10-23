"""Unique session prefixes prevent filelog content-fingerprint collisions.

Identical shell or CLI banners otherwise give separate logs the same leading
content. Only newly created files receive a header; existing logs stay intact.
"""

import os
from datetime import UTC, datetime
from pathlib import Path


def session_log_header(name: str, *, pid: int | None = None, now: datetime) -> bytes:
    """Return a UTF-8 session identity line with a UTC start timestamp."""
    start = now.isoformat()
    process = f" pid={pid}" if pid is not None else ""
    return f"--- meiva session {name} start={start}{process} ---\n".encode()


def open_session_log(path: Path, name: str, *, pid: int | None = None) -> tuple[int, bool]:
    """Open a transcript, writing its identity only when exclusively created."""
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return os.open(path, flags | os.O_APPEND), False
    try:
        created_at = datetime.fromtimestamp(os.fstat(fd).st_mtime, tz=UTC)
        header = session_log_header(name, pid=pid, now=created_at)
        while header:
            written = os.write(fd, header)
            if written == 0:
                raise OSError("Session log header write made no progress")  # noqa: TRY301 - close fd on failure
            header = header[written:]
    except BaseException:
        os.close(fd)
        raise
    return fd, True
