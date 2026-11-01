# meiva:note
# Asking this cluster's postmaster whether it is there: the probe, the three answers it can give,
# and the readiness callback a start waits on.
#
# Three answers, not two. "The server is not running" and "the probe itself did not run" are
# different facts, and one boolean cannot hold both: a probe whose program is missing, was stopped
# at its bound or was refused by the operating system would read as NOT_RUNNING, and a caller that
# only sees `False` decides a healthy server is down — starting a second postmaster on its port.
# The reading type is `instance/probe.py`; this module is where the program is run.
#
# The program gets a bound of its own (`-t`, in whole seconds) and the runner waits a little
# longer, so "no response" is the program's own answer (exit 2) rather than a probe killed on the
# boundary. That margin is not a supervision budget: it exists only so the program speaks first.

from __future__ import annotations

import subprocess

from base.host.system.hostport import Host
from cli.commands.data_plane.instance.probe import ProbeReading, ProbeState, pg_isready_reading

__all__ = ["PG_PROBE_TIMEOUT_S", "PostmasterWait", "launch_failure", "pg_running"]

# meiva:note
# How long a probe may take to answer when its caller names no bound. `pg_isready` uses the same
# three seconds as its own default, so this changes nothing for a server that answers.
PG_PROBE_TIMEOUT_S = 3.0

# meiva:note
# How much longer than its own `-t` the runner waits before it kills the probe: enough for the
# program to exit and be reaped, small enough that a probe which ignores its own limit cannot
# outlive the start that asked it.
_PROBE_KILL_MARGIN_S = 2.0


# meiva:note
# Ask `pg_isready` about the postmaster listening on `dial_host`:`pg_port`.
#
# A probe that cannot run answers UNANSWERED with its reason — a `pg_isready` that is missing, not
# executable, or had to be killed — and `pg_isready_reading` keeps the program's own "no attempt"
# (exit 3) in that same state. A start or stop decision must never read either as a server that is
# down.
def pg_running(
    pg_isready: str,
    pg_port: int,
    dial_host: str,
    *,
    host: Host,
    timeout_s: float = PG_PROBE_TIMEOUT_S,
) -> ProbeReading:
    bound_s = timeout_s + _PROBE_KILL_MARGIN_S
    try:
        out = host.run(
            [
                pg_isready,
                "-h",
                dial_host,
                "-p",
                str(pg_port),
                "-t",
                str(max(1, int(timeout_s))),
            ],
            capture_output=True,
            timeout=bound_s,
        )
    except subprocess.TimeoutExpired:
        # meiva:note
        # Stopped at the runner's bound: the server may still come up, so a waiting reader asks
        # again. Only the start timeout decides when to give up.
        return ProbeReading(ProbeState.UNANSWERED, f"pg_isready did not exit within {bound_s:g}s")
    except OSError as exc:
        # meiva:note
        # The program could not start at all (missing, not executable, refused by the OS). Waiting
        # cannot repair this, and an operator must read the probe's reason, not "not ready".
        return ProbeReading(
            ProbeState.UNANSWERED, f"pg_isready could not run: {exc}", retryable=False
        )
    return pg_isready_reading(out.returncode)


# meiva:note
# One Postgres start's readiness wait, as the caller holds it.
#
# `postgres.start(ready=...)` calls it until the postmaster answers or the start's own deadline
# passes, and afterwards the launch asks it what it learned. `answered` is the fact the failure
# report turns on: a probe that answered at least once — even "not running" — told the launch
# something about the server, and the timeout message stays the custody boundary's own literal; a
# probe that never answered is the only case that adds the probe's words, so a broken probe is never
# reported as a slow postmaster and a slow postmaster is never reported as a broken probe.
class PostmasterWait:
    def __init__(
        self,
        pg_isready: str,
        pg_port: int,
        dial_host: str,
        *,
        host: Host,
        timeout_s: float,
    ) -> None:
        self._pg_isready = pg_isready
        self._pg_port = pg_port
        self._dial_host = dial_host
        self._host = host
        self._timeout_s = timeout_s
        self._answered = False
        self._last_unanswered = ""
        # meiva:note
        # One probe before the start builds its wait: a probe that cannot run is a broken launch,
        # and it refuses here, with its own words and before any postmaster exists.
        first = self._probe()
        if first.state is ProbeState.UNANSWERED and not first.retryable:
            raise RuntimeError(
                f"cannot probe the postmaster on {dial_host}:{pg_port}: {first.detail}"
            )

    def _probe(self) -> ProbeReading:
        reading = pg_running(
            self._pg_isready,
            self._pg_port,
            self._dial_host,
            host=self._host,
            timeout_s=self._timeout_s,
        )
        if reading.state is ProbeState.UNANSWERED:
            self._last_unanswered = reading.detail
        else:
            self._answered = True
        return reading

    def __call__(self) -> bool:
        return self._probe().state is ProbeState.RUNNING

    # meiva:note
    # Whether the probe ever answered this wait; a launch that timed out asks before it says why.
    @property
    def answered(self) -> bool:
        return self._answered

    # meiva:note
    # The last thing the probe said when it had no answer, for the failure that adds it.
    @property
    def last_unanswered(self) -> str:
        return self._last_unanswered


# meiva:note
# A launch that never became ready, as the operator reads it: the custody boundary's message
# unchanged when the probe answered, and with the probe's own words when it never did.
def launch_failure(wait: PostmasterWait, failure: BaseException) -> str:
    detail = "" if wait.answered else wait.last_unanswered
    return str(failure) if not detail else f"{failure} (the probe has no answer: {detail})"
