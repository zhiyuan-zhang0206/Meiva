"""Locations and bounds shared by the pty-sessions service and its clients.

Everything lives under ``$MEIVA_HOME/run/``: the service's unix socket
(``pty-sessions.sock``), its instance lock (``pty-sessions.lock``), its ledger
(``pty-sessions.json``); transcripts stay at ``$MEIVA_HOME/logs/<name>.out.log``.
Old ``pty-close-notices.json`` files are retained only for inspection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from base.native_process.root_control.client import current_uid

# AF_UNIX sun_path bound: 104 bytes on macOS, 108 on Linux. A deeply nested
# $MEIVA_HOME (a long worktree path) pushes the natural socket path over it; the
# service then lives at a short fixed path keyed by the home, which the service
# and every client compute identically.
SUN_PATH_MAX = 100

# The roster unit (and root unit id) of the pty-sessions service.
SERVICE_UNIT = "pty-sessions"

# Terminal geometry defaults (the classic pane shape). Defined here so the service
# reads them without importing the screen module, whose import pulls pyte (kept lazy
# until a capture needs it).
DEFAULT_COLS = 120
DEFAULT_ROWS = 40

# Wire-protocol payload bounds for one capture/resize request
# (task #3696 exception inventory): protective limits, not user-facing tuning knobs — the
# configurable surface is only the capture *default* window
# (display.shell_capture_default_lines). The service clamps to them, and the client
# validates against CAPTURE_MAX_LINES before dialing, so one request can never
# ask for an unbounded payload.
CAPTURE_MAX_LINES = 100000
RESIZE_MAX = 10000


# meiva:note
# The pty-sessions service's unix socket in the run directory `run`; ``<home>/run/pty-sessions.sock``.
#
# `run` names the run directory explicitly, so a read-only scan that must not create
# the home can ask for it without one.
#
# A path over the sun_path bound falls back to ``/tmp/meiva-pty-<uid>/<digest>.sock``,
# keyed by the run directory. The directory is fixed rather than
# ``tempfile.gettempdir()`` because the service (launched by root) and its clients
# (agent processes with their own TMPDIR) must compute the same path; the service
# creates the directory owner-only (`fallback_dir`).
def service_socket_path(run: Path, *, uid: int | None = None) -> Path:
    natural = run / "pty-sessions.sock"
    if len(str(natural)) <= SUN_PATH_MAX:
        return natural
    digest = hashlib.sha1(f"{run}\0pty-sessions".encode(), usedforsecurity=False).hexdigest()
    return fallback_dir(uid=uid) / f"{digest[:12]}.sock"


# meiva:note
# Where a socket too long for its home lives: ``/tmp/meiva-pty-<uid>`` (owner-only).
def fallback_dir(*, uid: int | None = None) -> Path:
    owner_uid = current_uid() if uid is None else uid
    return Path("/tmp") / f"meiva-pty-{owner_uid}"  # noqa: S108 — a fixed, uid-keyed, owner-only directory: every process of the user must compute the same path


def lock_path(run: Path) -> Path:
    """The service's instance lock (``run/pty-sessions.lock``), held while it runs."""
    return run / "pty-sessions.lock"


def ledger_path(run: Path) -> Path:
    """The service's own ledger of live shell identities (``run/pty-sessions.json``)."""
    return run / "pty-sessions.json"


# meiva:note
# The session's byte transcript (``<logs>/<name>.out.log``).
def transcript_path(logs: Path, name: str) -> Path:
    return logs / f"{name}.out.log"
