# meiva:note
# The cluster health probe's verdicts: readings in, the round's report out.
#
# The shell reads every fact a round depends on into `HealthReadings`; `judge_health` returns
# the ordered checks with what each prints and which failure it signals. Every check runs on
# every round: a failing check never stops the others, and every failed check emits its own
# `health_probe_failing` event (check, failure class, message), repeated on every run while it
# stays failed. The probe itself decides nothing about notifying: Grafana alert rules turn the
# failing stream into graded notifications.
#
# The disk check keeps its position first when it fails: a full data volume can keep the
# gateway from starting, and the report must lead with that cause. Its healthy line prints in
# the position the check had before the data volume moved to the front.

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cli.commands.cluster.plans.provider_verdicts import ProviderCheck
from cli.commands.cluster.plans.service_probe_plan import Launched, LaunchRecord, LaunchUnreadable

__all__ = [
    "CheckResult",
    "Failure",
    "FailureClass",
    "GatewayReading",
    "HealthReadings",
    "HealthRules",
    "PopulationRead",
    "PopulationReading",
    "SourceTreeReading",
    "judge_health",
    "liveness_waits",
]


# meiva:note
# Why a check failed, as Grafana groups it. `ALERT_ONLY` is a failure that rolling code back
# cannot fix.
class FailureClass(Enum):
    CODE = "code"
    ENVIRONMENT = "environment"
    MAINTENANCE = "maintenance"
    ALERT_ONLY = "alert-only"


# meiva:note
# The `health_probe_failing` signal of one failed check.
@dataclass(frozen=True)
class Failure:
    message: str
    failure_class: FailureClass


# meiva:note
# One check's report: what it prints to each stream, and its failure signal if it failed.
@dataclass(frozen=True)
class CheckResult:
    check: str
    stdout: tuple[str, ...]
    stderr: tuple[str, ...]
    failure: Failure | None


# meiva:note
# The gateway's liveness after the retry budget, and, when it is down, whether the data plane
# behind it is itself unreachable.
@dataclass(frozen=True)
class GatewayReading:
    alive: bool
    data_plane_abnormal: bool


class PopulationRead(Enum):
    COUNTED = "counted"
    NO_ROW = "no-row"
    DB_UNREACHABLE = "db-unreachable"


# meiva:note
# How many agents are live (running or idling on a machine not judged offline), or why the
# count could not be had; `launched` is what the last start put in the root manifest.
@dataclass(frozen=True)
class PopulationReading:
    read: PopulationRead
    count: int
    launched: LaunchRecord


# meiva:note
# What the source-tree guard found: its own findings, or the reasons it could not look.
@dataclass(frozen=True)
class SourceTreeReading:
    guard_skipped: bool
    findings: tuple[str, ...]


# meiva:note
# Everything one round observed. `schema_skew` is True only for a real code/DB migration-set
# disagreement: a database that cannot be reached is NOT a schema failure (a transient
# pgbouncer blip must not fire a false schema alert while code and DB are in sync). A failure
# string or a violation list is empty when the check passed or does not apply.
@dataclass(frozen=True)
class HealthReadings:
    disk_fraction: float | None
    gateway: GatewayReading
    population: PopulationReading
    schema_skew: bool
    service_failures: tuple[str, ...]
    walg_failure: str | None
    walg_enabled: bool
    editable_violations: tuple[str, ...]
    source_tree: SourceTreeReading
    provider: tuple[ProviderCheck, ...]


# meiva:note
# What the round is asked to hold the readings to.
@dataclass(frozen=True)
class HealthRules:
    agent_min: int
    check_schema: bool
    disk_watermark: float


# meiva:note
# The waits between the attempts to reach the gateway: a short gateway or data-plane restart
# window is filtered before an outage is declared, and a failed round spends the budget
# exactly once (three attempts, two waits at the defaults), because the checks that run after
# a failure add no liveness work and no extra waiting (task #5064).
def liveness_waits(attempts: int, interval_s: float) -> tuple[float, ...]:
    if attempts < 1:
        raise ValueError(f"gateway liveness needs at least one attempt, got {attempts}")
    return (interval_s,) * (attempts - 1)


def _ok(check: str, line: str) -> CheckResult:
    return CheckResult(check, (line,), (), None)


def _failed(check: str, message: str, failure_class: FailureClass, *notes: str) -> CheckResult:
    return CheckResult(check, (), (message, *notes), Failure(message, failure_class))


# meiva:note
# Alert-only: a full data volume is the 2026-08-08 outage class (checkpoint growth filled the
# disk and the gateway could not start), but rolling the cluster back to a previous commit
# frees no disk space. The fraction is None when unmeasurable, and a disk-full alarm must
# never be synthesized from a broken measurement. Exactly at the watermark is healthy.
def _disk_failure(readings: HealthReadings, rules: HealthRules) -> CheckResult | None:
    fraction = readings.disk_fraction
    if fraction is None or fraction <= rules.disk_watermark:
        return None
    return _failed(
        "disk_usage",
        f"FAIL: disk usage — data volume {fraction:.1%} used "
        f"(watermark {rules.disk_watermark:.0%})",
        FailureClass.ALERT_ONLY,
    )


def _gateway(reading: GatewayReading) -> CheckResult:
    if reading.alive:
        return _ok("gateway_liveness", "  ✓ gateway liveness")
    failure_class = FailureClass.ENVIRONMENT if reading.data_plane_abnormal else FailureClass.CODE
    return _failed(
        "gateway_liveness",
        "FAIL: gateway liveness — health endpoint unreachable or non-200",
        failure_class,
        *(("  environment-class failure",) if reading.data_plane_abnormal else ()),
    )


# meiva:note
# A low count is code-class unless local intent explains it: a home whose last start did not
# launch the agent host is under maintenance (the signal is still emitted, carrying the class
# for the reader, so intent cannot hide a real global outage). A population query that could
# not run is an environment fault, not evidence that the running code regressed, and a launch
# record that cannot be read explains nothing.
def _population_class(reading: PopulationReading) -> FailureClass:
    if reading.read is PopulationRead.DB_UNREACHABLE:
        return FailureClass.ENVIRONMENT
    if reading.read is PopulationRead.NO_ROW:
        return FailureClass.CODE
    if isinstance(reading.launched, LaunchUnreadable):
        return FailureClass.CODE
    if isinstance(reading.launched, Launched) and "agent-host" in reading.launched.sessions:
        return FailureClass.CODE
    return FailureClass.MAINTENANCE


def _population(reading: PopulationReading, agent_min: int) -> CheckResult:
    if reading.read is PopulationRead.COUNTED and reading.count >= agent_min:
        return _ok("agent_population", f"  ✓ agent population (>= {agent_min})")
    failure_class = _population_class(reading)
    notes = (
        ("  local agent maintenance explains the low population",)
        if failure_class is FailureClass.MAINTENANCE
        else ("  environment-class failure",)
        if failure_class is FailureClass.ENVIRONMENT
        else ()
    )
    return _failed(
        "agent_population",
        f"FAIL: agent population — fewer than {agent_min} agent(s) running/idling",
        failure_class,
        *notes,
    )


def _services(failing: tuple[str, ...]) -> CheckResult:
    if not failing:
        return _ok("service_probe", "  ✓ service probes")
    return _failed(
        "service_probe",
        f"FAIL: service probe — not healthy: {', '.join(sorted(failing))}",
        FailureClass.ALERT_ONLY,
    )


# meiva:note
# Alert-only: a stuck or misconfigured archiver is fixed by repairing the archive path, not by
# rolling code back. Silent unless WAL-G is switched on.
def _walg(failure: str | None, *, enabled: bool) -> CheckResult | None:
    if failure is not None:
        return _failed("walg_archive", f"FAIL: {failure}", FailureClass.ALERT_ONLY)
    return _ok("walg_archive", "  ✓ WAL archiving") if enabled else None


# meiva:note
# Alert-only: the 2026-08-27 outage class (a worktree uv sync under a polluted VIRTUAL_ENV
# repointed the prod venv at disposable source), but rolling back code does not fix a venv
# record. Explicit installation repair owns recovery; this probe and ordinary startup never
# rewrite these records.
def _editable(violations: tuple[str, ...]) -> CheckResult:
    if not violations:
        return _ok("editable_install", "  ✓ editable install records")
    detail = (
        "prod venv editable install names non-allowlisted source or is inconsistent: "
        + "; ".join(violations)
    )
    return _failed(
        "editable_install", f"FAIL: editable install — {detail}", FailureClass.ALERT_ONLY
    )


# meiva:note
# Alert-only: a tampered checkout that a source-run home executes is the 2026-08-28 outage
# class (edited source broke `import meiva` for every agent on the box), but rolling back code
# does not undo an on-disk edit. The probe detects and alerts; it never writes. A guard that
# could not look is its own failure, never a clean tree.
def _source_tree(reading: SourceTreeReading) -> CheckResult:
    if not reading.findings:
        return _ok("source_tree", "  ✓ source tree integrity")
    joined = "; ".join(reading.findings)
    detail = (
        f"prod source tree guard skipped: {joined}"
        if reading.guard_skipped
        else f"prod source tree tampered: {joined}"
    )
    return _failed("source_tree", f"FAIL: source tree — {detail}", FailureClass.ALERT_ONLY)


# meiva:note
# Each provider check reports in the same run: a drained balance no longer stops the
# halted-agents check from reporting. A check's detail lines precede its failure line; with
# nothing failing the guard prints one healthy line.
def _provider(checks: tuple[ProviderCheck, ...]) -> tuple[CheckResult, ...]:
    results = tuple(
        CheckResult(
            check.check,
            (),
            (*check.notes, *(() if check.failure is None else (check.failure,))),
            None if check.failure is None else Failure(check.failure, FailureClass.ALERT_ONLY),
        )
        for check in checks
    )
    if any(check.failure is not None for check in checks):
        return results
    return (*results, _ok("provider_guard", "  ✓ provider account guard"))


def judge_health(readings: HealthReadings, rules: HealthRules) -> tuple[CheckResult, ...]:
    disk = _disk_failure(readings, rules)
    walg = _walg(readings.walg_failure, enabled=readings.walg_enabled)
    return (
        *((disk,) if disk is not None else ()),
        _gateway(readings.gateway),
        _population(readings.population, rules.agent_min),
        *(
            (
                _failed(
                    "schema",
                    "FAIL: schema health — applied version behind required (CodeBehindSchema)",
                    FailureClass.CODE,
                )
                if readings.schema_skew
                else _ok("schema", "  ✓ schema health"),
            )
            if rules.check_schema
            else ()
        ),
        _services(readings.service_failures),
        *((_ok("disk_usage", "  ✓ disk usage"),) if disk is None else ()),
        *((walg,) if walg is not None else ()),
        _editable(readings.editable_violations),
        _source_tree(readings.source_tree),
        *_provider(readings.provider),
    )
