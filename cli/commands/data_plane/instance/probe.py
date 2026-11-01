# meiva:note
# What a server liveness probe learned, in the one vocabulary Postgres and Redis share.
#
# Three states, not two. "The server is not running" and "the probe itself did not run" are
# different facts, and one boolean cannot hold both: a probe whose program is missing, was
# stopped at its bound or was refused by the operating system would read as NOT_RUNNING, and a
# caller that only sees `False` decides a healthy server is down — starting a second postmaster
# on its port, or restarting a database under its clients. The third state names that case, and
# `detail` carries the probe's own words about why it has no answer.
#
# A caller that needs a yes/no (a bounded readiness wait, a best-effort hint) says so at its own
# site, where the decision it makes is visible; the probe never makes that decision for it.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["ProbeReading", "ProbeState", "pg_isready_reading"]


class ProbeState(StrEnum):
    # meiva:note
    # The probe ran and the server answered it.
    RUNNING = "running"
    # meiva:note
    # The probe ran and the server gave no answer: nothing accepts connections on that address,
    # or what accepts them is not ready to serve.
    NOT_RUNNING = "not running"
    # meiva:note
    # The probe did not run: its program could not start, was stopped at its own bound, or itself
    # reports that it made no attempt. This says nothing about the server.
    UNANSWERED = "unanswered"


@dataclass(frozen=True)
class ProbeReading:
    state: ProbeState
    # meiva:note
    # Why an UNANSWERED probe has no answer, in the probe's own words; empty for the two answers.
    detail: str = ""
    # meiva:note
    # Whether asking again could produce a different answer. Only an UNANSWERED probe needs this
    # question: a reader that waits (a readiness loop) may keep asking while this is true, and must
    # stop with the probe's own reason when it is false — a probe whose program is missing or was
    # refused does not become answerable by waiting, and reading it as "not ready yet" turns a
    # broken probe into a slow server in the failure report.
    retryable: bool = True


# meiva:note
# What one `pg_isready` exit status says about the server.
#
# 0: accepting connections. 1: rejecting them (the postmaster is up but not ready to serve).
# 2: no response. 3: no attempt was made. The last one is the probe itself not running — bad
# parameters, a program that never started — so it is UNANSWERED, never "not running": the server
# told it nothing. Any other status (one the program does not document, a probe killed by a
# signal) is unanswered too.
#
# The program's own reading lives here, not in `pg_plan`: that module owns the decisions a start
# makes and is at its public-surface budget, and this maps a program's answer onto the shared
# vocabulary rather than deciding anything.
def pg_isready_reading(returncode: int) -> ProbeReading:
    match returncode:
        case 0:
            return ProbeReading(ProbeState.RUNNING)
        case 1 | 2:
            return ProbeReading(ProbeState.NOT_RUNNING)
        case 3:
            # meiva:note
            # "No attempt was made": the program refused its own arguments, which no wait repairs.
            return ProbeReading(
                ProbeState.UNANSWERED, "pg_isready made no attempt (exit 3)", retryable=False
            )
        case _:
            # meiva:note
            # A status the program does not document, or a signal death: the server may still come
            # up, so the reader may ask again.
            return ProbeReading(
                ProbeState.UNANSWERED, f"pg_isready left no answer (exit {returncode})"
            )
