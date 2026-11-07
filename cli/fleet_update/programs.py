# meiva:note
# The programs the window ships to the gateway host on stdin, and the one it asks about the
# whole cluster before and after the stops: the alert silence. The source of
# each is the operator's checkout, so the first update that carries a change to one already runs
# it. A reply that cannot be read costs a warning, never the window.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

from pathlib import Path

from .plan.silence_replies import silence_command, silence_warnings
from .session import Session

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
