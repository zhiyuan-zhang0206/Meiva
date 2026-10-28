# meiva:note
# Writing a session transcript's identity into the file that starts it.
#
# A transcript is opened once, exclusively, by whoever starts the session; a new file receives the
# identity line `log_prefix.session_log_header` renders, an existing one is appended to untouched.
# The shell owns the descriptor: it opens, writes the line and hands the descriptor back to the
# session that will write the child's output to it.

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from base.sessions.log_prefix import session_log_header


# meiva:note
# Open `path` for a session's transcript and write the identity line only to a file this call
# created; the descriptor and whether the file is new go back to the caller.
def open_session_log(path: Path, name: str, *, pid: int | None = None) -> tuple[int, bool]:
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
                raise OSError("Session log header write made no progress")  # noqa: TRY301
            header = header[written:]
    except BaseException:
        os.close(fd)
        raise
    return fd, True
