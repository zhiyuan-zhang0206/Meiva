# meiva:note
# Root-owned service health: bounded observation and spaced process restarts. Only
# observed DOWN results may request replacement; unknown identity, inspection
# errors, timeouts and foreign listeners stay terminal until fresh evidence
# resolves them. Consecutive replacements are spaced by one backoff ladder, and a
# unit that spends that budget opens a circuit breaker whose only escapes are the
# ladder's capped cadence and a dependency-recovery grant: one attempt on the
# first usable database this monitor observes and one per unavailable-to-answering
# edge (task #5315).
# meiva:link services/supervision/meiva_root/docs/meiva_root.meiva.okf.md

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Final, Protocol

from base.clock import Clock
from base.daemon.health import DaemonProbe, ProbeVerdict
from base.deploy.progress_timeout import (
    DEFAULT_STARTUP_GRACE_S,
    HEALTH_RESTART_LEASE_GRACE_S,
    ROOT_HEALTH_INTERVAL_S,
)
from base.native_process.ownership import OwnedProcess
from services.supervision.meiva_root.failure_state import (
    DEFAULT_OPERATOR_STOP_ALARM_S,
    FailureKind,
    LeaseGraceState,
    LeaseProbe,
    UnitFailureFacts,
    clear_lease_grace,
    consider_lease,
    derive_kind,
    describe,
    note_operator_stop,
    report_lease_recovered,
)
from services.supervision.meiva_root.manifest import UnknownUnitError
from services.supervision.meiva_root.probes import Probe, ProbeError, ProbeRegistry, ProbeRunner
from services.supervision.meiva_root.stopping import UnitStopTimeoutError

_log = logging.getLogger(__name__)


# meiva:note
# The continuous-outage retry ladder: the first replacement of a fresh outage stays
# immediate, and only consecutive failures are spaced — 60s, 5m, then 15m for every
# attempt after. On 2026-10-18 a fleet-wide database stall made every machine's
# `agent_host` exit within seconds; unspaced, each machine spent its three restarts
# in four minutes and then sat silent for 36 (task #5315).
DEFAULT_RESTART_BACKOFF_S: Final[tuple[float, ...]] = (60.0, 300.0, 900.0)


# meiva:note
# Seconds to wait before the attempt that follows `attempts` spent ones: none for
# the first of an outage, then the ladder entry for its index, capped at the last.
def restart_delay_s(attempts: int, ladder: Sequence[float]) -> float:
    if attempts <= 0:
        return 0.0
    return ladder[min(attempts - 1, len(ladder) - 1)]


class RevivalHost(Protocol):
    """The supervisor slice the health runner needs (duck-typed for stubs)."""

    async def restart(self, unit_id: str) -> dict[str, object]:
        """Replace a generation through the supervisor's bounded child lifecycle."""
        ...

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        """Retained native birth and monotonic start; never recaptured from a PID."""
        ...

    def revival_deferral(self, unit_id: str) -> str | None:
        """Why a would-be reviver must not act on `unit_id` now, or None."""
        ...

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        """The failure-state facts of one unit; unknown units raise UnknownUnitError."""
        ...

    # meiva:note
    # Sealed paths whose contents changed since admission; re-read every health round, because
    # the answer is otherwise invisible until a spawn fails (task #6403). Unknown units raise.
    def unit_input_drift(self, unit_id: str) -> tuple[str, ...]: ...


HealthGate = Callable[[], tuple[bool, str]]
"""A blocking-policy seam: `(allowed, why)`; absent means always allowed.

Deployment may supply an explicit admission decision. External transition
controllers do not run inside this service observer.
"""


# meiva:note
# One validation step per line: the rules a config enforces are independent of each other, and
# a reader checks a list of them rather than a cascade of branches.
def _require(*, condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Timing policy for the health runner (seconds unless noted)."""

    interval_s: float = ROOT_HEALTH_INTERVAL_S
    """Delay between completed rounds."""
    probe_timeout_s: float = 20.0
    """Maximum wait for one observation; an overdue worker is never duplicated."""
    startup_grace_s: float = DEFAULT_STARTUP_GRACE_S
    """Generic generation startup budget; deployment supplies per-unit readiness tiers."""
    # meiva:note
    # `max_restart_attempts` is what one continuous outage may spend before its
    # circuit breaker opens; spending it leaves only the ladder's capped cadence
    # (see DEFAULT_RESTART_BACKOFF_S) or a dependency-recovery signal.
    max_restart_attempts: int = 3
    restart_backoff_s: tuple[float, ...] = DEFAULT_RESTART_BACKOFF_S
    # meiva:note
    # How many consecutive rounds a unit whose intent says running and whose process is
    # gone must be seen that way before the absence itself is reported (task #6345).
    # Absence is the one failure kind that is a claim about nothing being there, so it has
    # to outlast the ordinary gaps: the root spawns every running unit before this
    # monitor's first round, and a replacement clears its old generation before the new one
    # exists. Three rounds at the deploy interval (60s) is about two minutes — longer than
    # a replacement, and inside the 3m look-back of the failure-state rules.
    absent_rounds_before_report: int = 3
    # meiva:note
    # How long the first attempt of one outage yields to a live impersonation lease; zero
    # disables the courtesy. The root passes `daemon.health_restart_lease_grace_s`; the
    # default here is the deploy family's own constant, never a restated literal.
    lease_grace_s: float = HEALTH_RESTART_LEASE_GRACE_S
    operator_stop_alarm_s: float = DEFAULT_OPERATOR_STOP_ALARM_S

    def __post_init__(self) -> None:
        _require(
            condition=math.isfinite(self.startup_grace_s) and self.startup_grace_s >= 0,
            message="startup_grace_s must be nonnegative",
        )
        _require(condition=self.probe_timeout_s > 0, message="probe_timeout_s must be positive")
        _require(condition=self.interval_s > 0, message="interval_s must be positive")
        _require(
            condition=self.max_restart_attempts >= 1,
            message="max_restart_attempts must be at least 1",
        )
        _require(
            condition=self.absent_rounds_before_report >= 1,
            message="absent_rounds_before_report must be at least 1",
        )
        _require(
            condition=math.isfinite(self.operator_stop_alarm_s) and self.operator_stop_alarm_s > 0,
            message="operator_stop_alarm_s must be positive",
        )
        _require(
            condition=bool(self.restart_backoff_s),
            message="restart_backoff_s must hold at least one delay",
        )
        _require(
            condition=all(math.isfinite(value) and value >= 0 for value in self.restart_backoff_s),
            message="restart_backoff_s delays must be finite and nonnegative",
        )
        _require(
            condition=math.isfinite(self.lease_grace_s) and self.lease_grace_s >= 0,
            message="lease_grace_s must be finite and nonnegative",
        )


@dataclass(slots=True)
class UnitHealth:
    """One unit's accumulated health state — the runner's whole memory of it."""

    generation: tuple[OwnedProcess, float] | None = None
    """Retained native generation to which the latest observation belongs."""
    generation_ready: bool = False
    """Whether this generation has produced a fresh ALIVE observation."""
    restart_attempts: int = 0
    """Actual automatic restart calls since ALIVE or an observed operator stop."""
    # meiva:note
    # `next_restart_at` is the monotonic deadline the next attempt waits for.
    # `breaker_open` is the *reported* edge, never the predicate (which is
    # `restart_attempts >= max_restart_attempts`): it remembers which side of the
    # transition was already signalled, so each crossing emits exactly one event.
    # `breaker_since` is the monotonic second it opened, for the close event.
    # `db_recovery_pending` is a dependency-recovery grant this unit has not spent
    # yet; it survives the rounds that cannot act on it (task #5315).
    next_restart_at: float | None = None
    grace: LeaseGraceState = field(default_factory=LeaseGraceState)
    breaker_open: bool = False
    breaker_since: float | None = None
    db_recovery_pending: bool = False
    # meiva:note
    # Consecutive rounds this unit has been observed with intent running and no process
    # (task #6345). The count IS the debounce, so any round that is not an absence resets
    # it — a unit must be absent now, and have been absent every round before now.
    absent_rounds: int = 0
    last_verdict: str | None = None
    """Value of the last probe verdict (`alive` / `down` / `port-taken` / `unavailable`)."""
    last_detail: str = ""
    """Detail string of the last probe verdict."""
    # meiva:note
    # Whether the last report was "answers its probe, owned by no generation here".
    unowned_alive: bool = False
    alarmed_stop_since: float | None = None


# Explicit stop is expected; every other deferral remains visible as a warning.
_HELD_DOWN = "held down"
_NO_REVIVAL_VERB = "no revival verb (not a tree unit)"
# meiva:note
# Unfinished-observation name of the breaker's dependency probe.
_DB_RECOVERY = "db-recovery"


class HealthMonitor:
    """One probe round over every registered unit, acting per policy.

    Rounds run sequentially — one round is a bounded stream of probes — and each
    unit reports expected probe unavailability as a verdict. Unexpected round
    errors propagate to the root lifecycle. Per-unit state lives in this
    process; a fresh process starts without observation history.
    """

    def __init__(
        self,
        supervisor: RevivalHost,
        registry: ProbeRegistry,
        *,
        config: HealthConfig | None = None,
        tasks: asyncio.TaskGroup | None = None,
        gate: HealthGate | None = None,
        startup_graces: Mapping[str, float] | None = None,
        db_recovery_probe: Probe | None = None,
        lease_probe: LeaseProbe | None = None,
        clock: Clock,
    ) -> None:
        self._supervisor = supervisor
        self._registry = registry
        self._config = config if config is not None else HealthConfig()
        self._gate = gate
        self._lease_probe = lease_probe
        self._db_recovery_probe = db_recovery_probe
        self._clock = clock
        self._db_runner = ProbeRunner()
        self._db_verdict: ProbeVerdict | None = None
        self._startup_graces = dict(startup_graces or {})
        if any(not math.isfinite(value) or value < 0 for value in self._startup_graces.values()):
            raise ValueError("startup grace overrides must be nonnegative")
        self._units: dict[str, UnitHealth] = {}
        self._runners: dict[str, ProbeRunner] = {}
        self._task: asyncio.Task[None] | None = None
        self._tasks = tasks
        self.unfinished_probes: tuple[str, ...] = ()
        self._closed = False

    async def run_round(self) -> None:
        """Probe every unit once, in registration order."""
        if self._closed:
            raise RuntimeError("root health admission closed")
        await self._grant_dependency_recovery()
        for unit_id in self._registry.unit_ids():
            await self._check_unit(unit_id)
            self._sync_breaker(unit_id, self._unit_state(unit_id))
        self._report_failure_states()
        self._report_input_drift()

    # meiva:note
    # One dependency probe per round while a breaker is open on a unit that is not
    # held down, and only then (a held-down unit must not be restarted, so the probe
    # by itself would be a database dial `meiva stop` cannot justify). A usable
    # database *grants* one immediate attempt per open breaker: on the first
    # observation (the monitor cannot tell a recovered database from one that never
    # failed) and on every unavailable-to-answering edge. The grant is a latch, not a
    # round's boolean: the rounds that cannot act on it — a terminal verdict, a
    # startup grace — must not consume it, or a database that
    # recovers in one of them costs a full capped interval. A database observed
    # failing withdraws the grant, and a unit that never held one keeps its cadence.
    async def _grant_dependency_recovery(self) -> None:
        probe = self._db_recovery_probe
        waiting = [
            state
            for unit_id, state in self._units.items()
            if state.breaker_open and self._deferral(unit_id) != _HELD_DOWN
        ]
        if probe is None or not waiting:
            self._db_verdict = None
            return
        result = await self._db_runner.observe(probe, self._config.probe_timeout_s)
        previous = self._db_verdict
        self._db_verdict = result.verdict
        if not result.alive:
            for state in waiting:
                state.db_recovery_pending = False
            _log.info(
                "[health] database still unavailable while a breaker is open (%s)", result.detail
            )
            return
        if previous is ProbeVerdict.ALIVE:
            return
        for state in waiting:
            state.db_recovery_pending = True
        if previous is None:
            _log.warning(
                "[health] database answers while a breaker is fresh — one immediate attempt"
            )
        else:
            _log.warning(
                "[health] database answered again — every open unit breaker may restart once now"
            )

    def _report_failure_states(self) -> None:
        """Emit one `root_unit_failure_state` per unit sitting in a failure state.

        The condition is a state, so it is reported on every round while it
        holds. Units outside the supervisor's tree (the static probe path) carry
        no failure facts and are skipped.
        """
        for unit_id in self._registry.unit_ids():
            try:
                facts = self._supervisor.unit_failure_facts(unit_id)
            except UnknownUnitError:
                continue
            state = self._unit_state(unit_id)
            kind = self._confirmed_kind(
                unit_id,
                state,
                derive_kind(
                    facts,
                    restart_limit_reached=state.restart_attempts
                    >= self._config.max_restart_attempts,
                ),
            )
            if kind is None:
                continue
            from base.log import logger

            logger.warning(
                "[health] unit {unit}: failure state {kind} ({detail})",
                event="root_unit_failure_state",
                unit=unit_id,
                kind=kind.value,
                detail=describe(facts, kind, state.last_detail),
                attempts=state.restart_attempts,
                last_error=facts.last_error or "",
            )

    # meiva:note
    # Report a unit this root could no longer spawn: its declared input changed since admission,
    # so every later birth of it — a manual restart, this monitor's own replacement — is refused
    # until a new root generation or an explicit re-admission (task #6403). Nothing else says so:
    # until something tries to spawn the unit, the drift is invisible, and the unit looks
    # healthy right up to the moment it cannot be revived.
    #
    # Only a unit whose intent says running is reported: a stopped unit that cannot be spawned
    # is a fact about a decision nobody has made yet, not a degradation. The condition is a
    # state, emitted every round while it holds.
    def _report_input_drift(self) -> None:
        for unit_id in self._registry.unit_ids():
            try:
                drift = self._supervisor.unit_input_drift(unit_id)
                facts = self._supervisor.unit_failure_facts(unit_id)
            except UnknownUnitError:
                continue
            if not drift or not facts.intent_running:
                continue
            from base.log import logger

            for path in drift:
                logger.warning(
                    "[health] unit {unit}: declared input {path} changed since admission — this "
                    "root refuses to spawn it again",
                    event="root_unit_inputs_changed",
                    unit=unit_id,
                    path=path,
                )

    # meiva:note
    # The kind this round reports, which is not always the kind this round derived: a
    # recorded failure is a fact and is reported at once, while absence is a claim about
    # nothing being there and must survive `absent_rounds_before_report` consecutive
    # rounds first (task #6345). Absence that something else already explains is not
    # reported a second time: a probe that answers while this root owns no generation is
    # `root_unit_unowned_alive`, and a unit whose policy forbids revival is
    # `root_unit_not_revivable` on this same round.
    def _confirmed_kind(
        self, unit_id: str, state: UnitHealth, kind: FailureKind | None
    ) -> FailureKind | None:
        if kind is not FailureKind.ABSENT:
            state.absent_rounds = 0
            return kind
        if state.unowned_alive or self._deferral(unit_id) is not None:
            state.absent_rounds = 0
            return None
        state.absent_rounds += 1
        if state.absent_rounds < self._config.absent_rounds_before_report:
            return None
        return kind

    async def start(self) -> None:
        """Run rounds until `stop()` — sleep first, then one round per interval."""
        if self._task is not None:
            raise RuntimeError("health monitor already started")
        if self._closed:
            raise RuntimeError("root health admission closed")
        if self._tasks is None:
            raise RuntimeError("monitor requires the root participant task group")
        self._task = self._tasks.create_task(self._loop())

    async def stop(self) -> None:
        """Close probe admission and stop all owned work within one join budget."""
        self._closed = True
        for runner in self._runners.values():
            runner.close()
        self._db_runner.close()
        task = self._task
        self._task = None
        failures: list[BaseException] = []
        if task is not None:
            task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await task
            except BaseException as exc:
                failures.append(exc)
        deadline = self._clock.monotonic() + 0.2
        unfinished: list[str] = []
        for unit_id, runner in self._runners.items():
            try:
                if not runner.stop(max(0.0, deadline - self._clock.monotonic())):
                    unfinished.append(unit_id)
            except BaseException as exc:
                failures.append(exc)
        try:
            if not self._db_runner.stop(max(0.0, deadline - self._clock.monotonic())):
                unfinished.append(_DB_RECOVERY)
        except BaseException as exc:
            failures.append(exc)
        self.unfinished_probes = tuple(unfinished)
        if unfinished:
            _log.warning("root health stopped with unfinished observations: %s", unfinished)
        if failures:
            raise BaseExceptionGroup("root health teardown failed", failures)

    def snapshot(self) -> dict[str, UnitHealth]:
        """A copy of every known unit's health state, keyed by unit id."""
        return {unit_id: replace(state) for unit_id, state in self._units.items()}

    def health_snapshot(self) -> dict[str, object]:
        """A plain-dict view for `Supervisor.status()` (the B7 health surface)."""
        units: dict[str, object] = {}
        for unit_id, state in self._units.items():
            units[unit_id] = {
                "startup_remaining_s": self._startup_remaining(unit_id, state),
                "generation_ready": state.generation_ready,
                "restart_attempts": state.restart_attempts,
                "restart_limit_reached": (
                    state.restart_attempts >= self._config.max_restart_attempts
                ),
                "breaker_open": state.breaker_open,
                "db_recovery_pending": state.db_recovery_pending,
                "next_restart_in_s": self._restart_due_in_s(state),
                "last_verdict": state.last_verdict,
                "last_detail": state.last_detail,
                "unowned_alive": state.unowned_alive,
            }
        return units

    # meiva:note
    # Seconds until this unit's next attempt may start, or None when none is
    # pending; an unspent dependency-recovery grant makes it due now.
    def _restart_due_in_s(self, state: UnitHealth) -> float | None:
        if state.db_recovery_pending:
            return 0.0
        if state.next_restart_at is None:
            return None
        return max(0.0, state.next_restart_at - self._clock.monotonic())

    # meiva:note
    # A stopped tree holds every unit down, which resets its breaker, so the
    # dependency probe stays idle for the whole stop window.
    async def _loop(self) -> None:
        while True:
            await self._clock.asleep(self._config.interval_s)
            await self.run_round()

    # ── one unit ─────────────────────────────────────────────────────────────

    def _resolvable_probe(self, unit_id: str, state: UnitHealth) -> Probe | None:
        """Resolve the unit's probe, or report the no-verdict case and answer None."""
        try:
            return self._registry.resolve(unit_id)
        except ProbeError as exc:
            state.last_verdict = "unavailable"
            state.last_detail = str(exc)
            _log.error(
                "[health] unit %s: no resolvable probe (%s) — no verdict, no action",
                unit_id,
                exc,
            )
            return None

    async def _check_unit(self, unit_id: str) -> None:
        state = self._unit_state(unit_id)
        probe = self._resolvable_probe(unit_id, state)
        if probe is None:
            return
        result = await self._probe(unit_id, probe)
        state.last_verdict = result.verdict.value
        state.last_detail = result.detail

        if result.alive:
            report_lease_recovered(
                state.grace,
                unit_id=unit_id,
                attempt=state.restart_attempts + 1,
                window_s=self._config.lease_grace_s,
                now=self._clock.monotonic(),
            )
            self._note_alive_ownership(unit_id, state)
            self._reset(state)
            state.alarmed_stop_since = None
            _log.debug("[health] unit %s: alive (%s)", unit_id, result.detail)
            return

        deferral = self._deferral(unit_id)
        if deferral == _HELD_DOWN:
            self._reset(state)
            self._note_operator_stop(unit_id, state, result)
            return

        if result.terminal:
            self._report_terminal(unit_id, result.detail)
            return

        startup_remaining = self._startup_remaining(unit_id, state)
        if startup_remaining > 0:
            _log.info(
                "[health] unit %s: %s (%s); startup has %.1fs remaining — observation only",
                unit_id,
                result.verdict.value,
                result.detail,
                startup_remaining,
            )
            return

        if self._gate is not None:
            allowed, why = self._gate()
            if not allowed:
                _log.info(
                    "[health] unit %s: down (%s) but %s — not restarting this round",
                    unit_id,
                    result.detail,
                    why,
                )
                return
        if deferral is not None:
            self._report_terminal(unit_id, f"{result.detail}; restart deferred: {deferral}")
            return
        breaker_open = state.restart_attempts >= self._config.max_restart_attempts
        if not self._attempt_due(state):
            return
        deadline = consider_lease(
            state.grace,
            probe=self._lease_probe,
            window_s=self._config.lease_grace_s,
            unit_id=unit_id,
            attempt=state.restart_attempts + 1,
            now=self._clock.monotonic(),
        )
        if deadline is not None:
            state.next_restart_at = deadline
            return
        # meiva:note
        # The window closed without a recovery: this outage's courtesy is over, and the
        # restart that follows must not later report itself as a cancelled one.
        state.grace.waiting = None
        recovery = state.db_recovery_pending
        state.db_recovery_pending = False
        state.restart_attempts += 1
        delay_s = restart_delay_s(state.restart_attempts, self._config.restart_backoff_s)
        state.next_restart_at = self._clock.monotonic() + delay_s
        if breaker_open and recovery:
            _log.warning(
                "[health] unit %s: down (%s), the database answered — restarting now "
                "(the breaker closes when a generation is alive)",
                unit_id,
                result.detail,
            )
        elif breaker_open:
            _log.warning(
                "[health] unit %s: down (%s), restarting on the breaker's %.0fs cadence "
                "(%d attempts)",
                unit_id,
                result.detail,
                delay_s,
                state.restart_attempts,
            )
        else:
            _log.warning(
                "[health] unit %s: down (%s), restarting (%d/%d)",
                unit_id,
                result.detail,
                state.restart_attempts,
                self._config.max_restart_attempts,
            )
        try:
            await self._supervisor.restart(unit_id)
        except* UnitStopTimeoutError as exc:
            # Supervisor retains the generation and records restart_failed.down.
            # This attempt remains spent; other errors still reach root's owner.
            _log.warning("[health] unit %s: replacement stop refused", unit_id, exc_info=exc)
        # Spawn acceptance is not readiness. The next ordinary round observes it.

    def _note_operator_stop(self, unit_id: str, state: UnitHealth, result: DaemonProbe) -> None:
        try:
            facts = self._supervisor.unit_failure_facts(unit_id)
        except UnknownUnitError:
            return
        now = self._clock.now()
        state.alarmed_stop_since = note_operator_stop(
            unit_id,
            facts,
            now.timestamp(),
            self._config.operator_stop_alarm_s,
            result.verdict.value,
            result.detail,
            state.alarmed_stop_since,
        )

    # meiva:note
    # Only two things restart a unit behind an open breaker: the ladder's capped
    # cadence coming due, and an unspent dependency-recovery grant. The grant turns a
    # fifteen-minute wait back into the two minutes the 2026-10-18 outage needed. A
    # held attempt is not silent either: the per-round failure-state signal reports it.
    def _attempt_due(self, state: UnitHealth) -> bool:
        if state.db_recovery_pending:
            return True
        return state.next_restart_at is None or self._clock.monotonic() >= state.next_restart_at

    # meiva:note
    # Signal each circuit-breaker transition exactly once: the per-round
    # `root_unit_failure_state` stream carries the condition while it holds (the
    # sustained alert), while these two events carry the failure count, the last
    # error and the time the breaker held.
    def _sync_breaker(self, unit_id: str, state: UnitHealth) -> None:
        open_now = state.restart_attempts >= self._config.max_restart_attempts
        if open_now == state.breaker_open:
            return
        state.breaker_open = open_now
        from base.log import logger

        if open_now:
            state.breaker_since = self._clock.monotonic()
            logger.warning(
                "[health] unit {unit}: circuit breaker OPEN after {attempts} failed automatic "
                "restarts — only the ladder's capped cadence or a database recovery restarts it "
                "now ({detail})",
                event="root_unit_breaker_open",
                unit=unit_id,
                attempts=state.restart_attempts,
                detail=self._last_error(unit_id, state),
            )
            return
        held_for = (
            0.0 if state.breaker_since is None else self._clock.monotonic() - state.breaker_since
        )
        state.breaker_since = None
        logger.info(
            "[health] unit {unit}: circuit breaker CLOSED after {open_for_s:.0f}s ({detail})",
            event="root_unit_breaker_closed",
            unit=unit_id,
            open_for_s=max(0.0, held_for),
            detail=self._last_error(unit_id, state),
        )

    # meiva:note
    # The unit's recorded exit error, else the detail of its last observation.
    def _last_error(self, unit_id: str, state: UnitHealth) -> str:
        try:
            facts = self._supervisor.unit_failure_facts(unit_id)
        except UnknownUnitError:
            return state.last_detail
        return facts.last_error or state.last_detail

    @staticmethod
    def _report_terminal(unit_id: str, detail: str) -> None:
        """Report a non-restartable observation without replenishing the budget."""
        from base.log import logger

        logger.warning(
            "[health] unit {unit}: NOT REVIVABLE by this unit ({detail}) — not restarting; "
            "resolve the reported failure before retrying",
            event="root_unit_not_revivable",
            unit=unit_id,
            detail=detail,
        )

    # meiva:note
    # An alive verdict says "this home's daemon of this unit answers" — never "this
    # root owns it". A leftover of a root that exited without stopping its tree
    # answers exactly the same way, which is how 11 units read "state stopped /
    # probe alive" for a day with every monitor green (task #5474). The event is
    # the only signal for a process no spawn record identifies; a recorded one is
    # stopped by the supervisor before it spawns (supervisor._clear_left_behind).
    def _note_alive_ownership(self, unit_id: str, state: UnitHealth) -> None:
        unowned = state.generation is None
        if unowned == state.unowned_alive:
            return
        state.unowned_alive = unowned
        from base.log import logger

        try:
            if unowned:
                logger.warning(
                    "unit {unit}: readiness probe answers but this root owns no generation — "
                    "the answering process is unmanaged and holds this unit's port",
                    event="root_unit_unowned_alive",
                    unit=unit_id,
                    verdict=state.last_verdict,
                    detail=state.last_detail,
                )
            else:
                logger.info(
                    "unit {unit}: the answering process is this root's generation again",
                    event="root_unit_owned_alive",
                    unit=unit_id,
                )
        except Exception:
            _log.exception("unit %s: ownership event fan-out failed", unit_id)

    def _deferral(self, unit_id: str) -> str | None:
        """Probe-only units are observation targets, with no restart authority."""
        try:
            return self._supervisor.revival_deferral(unit_id)
        except UnknownUnitError:
            return _NO_REVIVAL_VERB

    async def _probe(self, unit_id: str, probe: Probe) -> DaemonProbe:
        """Inspection failures are unavailable evidence, never permission to restart."""
        if self._closed:
            raise RuntimeError("root health admission closed")
        runner = self._runners.setdefault(unit_id, ProbeRunner())
        before = self._generation(unit_id)
        result = await runner.observe(probe, self._config.probe_timeout_s)
        after = self._generation(unit_id)
        state = self._unit_state(unit_id)
        if state.generation != after:
            state.generation = after
            state.generation_ready = False
        if before != after:
            return DaemonProbe.unavailable("service generation changed during observation")
        if result.alive:
            state.generation_ready = True
        return result

    def _generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        try:
            return self._supervisor.health_generation(unit_id)
        except UnknownUnitError:
            return None  # Static observers have no startup generation or revival verb.

    def _startup_remaining(self, unit_id: str, state: UnitHealth) -> float:
        if state.generation is None or state.generation_ready:
            return 0.0
        grace = self._startup_graces.get(unit_id, self._config.startup_grace_s)
        return max(0.0, state.generation[1] + grace - self._clock.monotonic())

    def _unit_state(self, unit_id: str) -> UnitHealth:
        state = self._units.get(unit_id)
        if state is None:
            state = UnitHealth()
            self._units[unit_id] = state
        return state

    @staticmethod
    def _reset(state: UnitHealth) -> None:
        """A healthy observation or expected stop ends the continuous outage."""
        state.restart_attempts = 0
        state.next_restart_at = None
        state.db_recovery_pending = False
        clear_lease_grace(state.grace)
