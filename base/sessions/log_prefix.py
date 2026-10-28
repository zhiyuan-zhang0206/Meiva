# meiva:note
# The identity line of a session transcript.
#
# Identical shell or CLI banners otherwise give separate logs the same leading content, so a new
# transcript starts with one line naming the session and the instant it began. Rendering it is a
# function of the name, the process id and the instant; `log_writer.open_session_log` is what
# writes it into the file it creates.

from __future__ import annotations

from datetime import datetime


# meiva:note
# The bracketed-free identity line for session `name`: its UTF-8 bytes, with the process id when
# the caller knows it and the UTC start instant in ISO form.
def session_log_header(name: str, *, pid: int | None = None, now: datetime) -> bytes:
    start = now.isoformat()
    process = f" pid={pid}" if pid is not None else ""
    return f"--- meiva session {name} start={start}{process} ---\n".encode()
