# meiva:note
# What `meiva restart` refuses, in what order it runs its legs, and what each leg's result means.
#
# A restart is four legs: the preflight probes and the start-readiness preflight (both refuse
# before anything stops), the stop leg and the start leg. The shell runs a leg and asks
# `settle_leg` what its result means; the choices the stop leg runs with are decided here.

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE
from cli.commands.lifecycle.stop.choices import StopChoices, StopRequest, StopWindow

__all__ = [
    "AGENT_HOST",
    "NO_ROOT_REFUSAL",
    "Continue",
    "Finish",
    "Leg",
    "Refusal",
    "RestartRequest",
    "StartLeg",
    "budget_line",
    "missing_unit_refusal",
    "restart_refusal",
    "restart_start_leg",
    "restart_stop_request",
    "settle_leg",
]

AGENT_HOST = "agent-host"


# meiva:note
# One `meiva restart` of the service tree as the command line states it.
#
# Exactly one of the three selections names the services the start leg launches; `timeout`
# bounds the stop leg and `start_timeout` the start leg's readiness wait.
@dataclass(frozen=True)
class RestartRequest:
    only_services: tuple[str, ...]
    disabled_services: tuple[str, ...]
    all_services: bool
    keep_shell_sessions: bool
    keep_browser: bool
    restart_extras: bool
    force: bool
    timeout: float
    start_timeout: float


# meiva:note
# The legs of a restart, in the order they run.
class Leg(Enum):
    PROBES = ("preflight", "\n→ preflight probes (validate-before-kill)")
    READINESS = (
        "preflight",
        "\n→ start readiness preflight (validate-before-kill, local state)",
    )
    STOP = ("stop", "")
    START = ("start", "")

    # meiva:note
    # The journal phase the leg's work is recorded under.
    @property
    def phase(self) -> str:
        return self.value[0]

    # meiva:note
    # What the restart prints before the leg runs ("" for none).
    @property
    def heading(self) -> str:
        return self.value[1]


# meiva:note
# A restart that must not begin; `message` is what the operator reads on stderr.
@dataclass(frozen=True)
class Refusal:
    message: str


# meiva:note
# Why a restart must not begin here, or None.
#
# Admission precedes even the restart journal: an incompatible caller must leave the running
# generation untouched.
def restart_refusal(exec_domain: str | None, supervised: str | None) -> Refusal | None:
    if exec_domain is not None:
        # meiva:note
        # An exec-domain restart is SIGKILLed by the execute_code call's own teardown, the
        # call's whole process group as its turn ends (nohup/& do not leave the group; 2026-09-12:
        # macmini stranded mid-stop). The restart closes persistent shell sessions like a stop,
        # so no shell session can host it either: a plain login shell does.
        return Refusal(
            f"  ✗ refusing restart: this process runs inside an agent execute_code "
            f"exec domain ({exec_domain}) — its process group is SIGKILLed when the "
            "call's turn ends, this restart with it — the host stranded mid-restart. Run "
            "it from a shell no meiva session hosts (e.g. a plain ssh/login shell)."
        )
    if supervised is not None:
        # meiva:note
        # The stop below kills every service session's tree, so a restart hosted inside one of
        # them severs itself mid stop→start and leaves the host down. The detached orchestration
        # sessions (the updater ladder's own `meiva restart`) are exempt.
        return Refusal(
            f"  ✗ refusing restart: this process runs inside supervised session "
            f"{supervised!r}, which the stop leg kills — tree included, this restart with "
            "it, leaving the host stopped. Run it from a shell no meiva session hosts "
            "(e.g. a plain ssh/login shell) — host still serving"
        )
    return None


# meiva:note
# The stop leg of a restart.
#
# Restart replaces the services and keeps the private data plane: its start leg needs the
# database. Shell sessions, the browser and the extras (Gate, the permissions helper, native
# LGTM) are kept or restarted as the operator chose, and no service is kept besides. A host
# without the gateway capability has no local data plane. A restart announces nothing and asks
# nothing (it runs non-interactively, from the detached updater too): the host comes straight
# back. `timeout` bounds this leg only; the start leg has its own.
def restart_stop_request(request: RestartRequest, *, roles: frozenset[str]) -> StopRequest:
    choices = StopChoices(
        keep_infra=True if "gateway" in roles else None,
        preserve_sessions=frozenset(),
        keep_browser=request.keep_browser,
        keep_shell_sessions=request.keep_shell_sessions,
        announce=False,
        teardown_extras=request.restart_extras,
        force=request.force,
    )
    return StopRequest(
        choices,
        StopWindow(for_seconds=None, reason=None),
        require_confirmation=False,
        timeout=request.timeout,
    )


# meiva:note
# What the start leg of a restart launches and how long it waits for readiness.
@dataclass(frozen=True)
class StartLeg:
    only_services: tuple[str, ...]
    disabled_services: tuple[str, ...]
    all_services: bool
    reconcile: bool
    ready_timeout: float


# meiva:note
# The start leg launches exactly the operator's named selection, within `start_timeout`.
#
# Units the stop leg kept still run, so the live root is not reconciled to the selection.
def restart_start_leg(request: RestartRequest) -> StartLeg:
    return StartLeg(
        only_services=request.only_services,
        disabled_services=request.disabled_services,
        all_services=request.all_services,
        reconcile=False,
        ready_timeout=request.start_timeout,
    )


def budget_line(timeout: float, start_timeout: float, journal_path: str) -> str:
    return (
        f"[meiva restart] budget contract: stop up to {timeout:.0f}s + "
        f"start readiness up to {start_timeout:.0f}s + bounded preflight probes; "
        "an outer caller's timeout must exceed this total. Status journal: "
        f"{journal_path} (phase progress and the final diagnosis "
        "stay readable there even if this process is cut off)"
    )


# meiva:note
# The leg passed; run the next one.
@dataclass(frozen=True)
class Continue: ...


# meiva:note
# The restart ends here with exit code `rc`.
#
# `error` is what the journal records (None for success); `notice` is the line printed on
# stderr first, when the leg's refusal has words of its own.
@dataclass(frozen=True)
class Finish:
    rc: int
    error: str | None
    notice: str | None


# meiva:note
# What leg `leg` returning `rc` means for the rest of the restart.
#
# A refusal before the stop stops nothing, so it declines with its own exit code: "nothing
# was stopped, host still serving" is what the detached updater must not run `meiva start`
# over. A stop leg that fails is the command's result, and no compensating start runs. The
# start leg's result is the restart's.
def settle_leg(leg: Leg, rc: int) -> Continue | Finish:
    match leg:
        case Leg.PROBES:
            if rc == 0:
                return Continue()
            return Finish(
                RESTART_DECLINED_EXIT_CODE,
                "preflight probes failed",
                "  ✗ refusing restart: preflight probes failed — host still serving",
            )
        case Leg.READINESS:
            # meiva:note
            # Every category the readiness preflight refuses on would also fail this restart's
            # own start leg, so a refusal never blocks a viable bounce.
            if rc == 0:
                return Continue()
            return Finish(
                RESTART_DECLINED_EXIT_CODE,
                "start-readiness preflight failed",
                "  ✗ refusing restart: start-readiness preflight failed — host still serving; "
                "nothing was stopped. Fix the findings above, then retry.",
            )
        case Leg.STOP:
            return Continue() if rc == 0 else Finish(rc, "stop leg failed", None)
        case Leg.START:
            return Finish(rc, None if rc == 0 else f"start leg failed with rc={rc}", None)


NO_ROOT_REFUSAL = (
    "  ✗ refusing restart: no live meiva-root on this unit; start it with `meiva start`"
)


# meiva:note
# Why the live root's units (their ids) leave `meiva restart --only agent-host` nothing to
# restart, or None.
def missing_unit_refusal(units: tuple[str, ...]) -> str | None:
    if AGENT_HOST not in units:
        return f"  ✗ refusing restart: the live root runs no {AGENT_HOST} unit"
    return None
