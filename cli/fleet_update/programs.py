# meiva:note
# The programs the window ships to the gateway host on stdin, and the two it asks about the
# whole cluster before and after the stops: the alert silence and the lease table. The source of
# each is the operator's checkout, so the first update that carries a change to one already runs
# it. A reply that cannot be read costs a warning, never the window.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import time
from pathlib import Path

from .plan.host_commands import PROGRAM
from .plan.lease_announcement import (
    LeaseReading,
    announcement,
    countdown,
    countdown_line,
    dry_run_line,
    read_leases,
    unread_warning,
)
from .plan.silence_replies import silence_command, silence_warnings
from .session import FailedError, Session

VERIFY_SCRIPTS = Path(__file__).resolve().parents[1] / "commands" / "management"
CHECK_ROWS_SCRIPT = Path(__file__).with_name("schedules_verify_check_rows.py")
ALERT_SILENCE_SCRIPT = Path(__file__).resolve().parents[1] / "fleet_alert_silence.py"


def program_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# meiva:note
# Open or close the window's alert silence on the gateway host. The program says
# `SILENCE failed` or `SILENCE skipped` when it cannot, and the window warns and goes on.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def run_silence(
    s: Session, gateway: str, verb: str, *, hours: float | None = None, comment: str = ""
) -> None:
    out = s.run(
        gateway,
        silence_command(verb, hours=hours, comment=comment),
        stdin=program_text(ALERT_SILENCE_SCRIPT),
    )
    for warning in silence_warnings(verb, out):
        s.say(warning)


def _read_leases(s: Session, gateway: str) -> LeaseReading:
    try:
        out = s.run(
            gateway,
            PROGRAM,
            effect=False,
            stdin=program_text(VERIFY_SCRIPTS / "fleet_leases.py"),
        )
    except FailedError as exc:
        return LeaseReading((), str(exc))
    return read_leases(out)


# meiva:note
# Name the takeovers the stops would end, before anything stops -- under --dry-run too -- and
# hold the stops open for as long as the operator asked: 0 reports the leases and returns at once.
# meiva:link cli/docs/fleet-update-window/lease-announcement.meiva.okf.md
def announce_leases(s: Session, gateway: str, confirm_seconds: int) -> None:
    reading = _read_leases(s, gateway)
    if reading.unread is not None:
        s.say(unread_warning(reading.unread))
        return
    for line in announcement(reading.leases):
        s.say(line)
    if not reading.leases or not confirm_seconds:
        return
    if s.dry_run:
        s.say(dry_run_line(confirm_seconds))
        return
    for tick in countdown(confirm_seconds, s.pacing.lease_tick_s):
        s.say(countdown_line(tick))
        time.sleep(tick.sleep_s)
