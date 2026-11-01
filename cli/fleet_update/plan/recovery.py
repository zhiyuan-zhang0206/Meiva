# meiva:note
# What a window does about a stop that failed part-way: the hosts it took down come back, on the
# OLD commit they still carry, so a failed window costs an aborted update instead of an outage.
# Three facts decide one host, and none of them alone is enough: it was serving when the window
# began (a host the operator had already stopped stays stopped), it is down now, and this run is
# what took it down -- either its stop completed here, or the host's own stop journal records a
# failed or unfinished stop. A host that is still running is never touched, and no code is rolled
# back: the switch phase never started.
# meiva:link cli/docs/fleet-update-window/failed-stop-recovery.meiva.okf.md

from __future__ import annotations

from enum import Enum

from .journal import HostEvidence, stop_failed


class Recovery(Enum):
    RUNNING = "running"
    LEFT_DOWN = "left-down"
    RESTART = "restart"


# meiva:note
# A host that is up needs nothing. A host that is down and this run took down (its stop completed
# here, or it is a host whose stop failed and its journal records a failed or unfinished stop)
# restarts; any other down host is left for the operator.
def recovery_action(
    *, alias: str, stopped: frozenset[str], failed: frozenset[str], evidence: HostEvidence
) -> Recovery:
    if evidence.daemon_up:
        return Recovery.RUNNING
    if alias in stopped or (alias in failed and stop_failed(evidence.journal)):
        return Recovery.RESTART
    return Recovery.LEFT_DOWN


# meiva:note
# The two lines that close the recovery: who is serving again, and who still needs an operator.
# Every host that is down and was not brought back is named, whether its start failed or this run
# could not claim it.
def recovery_report(down: tuple[str, ...], recovered: tuple[str, ...]) -> tuple[str, str]:
    manual = ", ".join(alias for alias in down if alias not in recovered) or "none"
    return (f"recovered: {', '.join(recovered) or 'none'}", f"needs manual: {manual}")
