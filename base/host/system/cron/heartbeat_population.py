# meiva:note
# The declared population of generated OS jobs, and what this machine expects of it.
#
# Every generated job definition lands in one scheduler namespace, so the expectation set has to
# be one list, not one file per job: `com.meiva.walg` is declared here even on a machine whose
# configuration leaves it uninstalled, and a reader elsewhere can then compute both directions —
# declared but not installed, installed but not declared — instead of only the second.
#
# The list is written out rather than discovered: a module that adds a fifth generated job is not
# in it, and `verified` refuses to render that job until it is. A test walks the package for
# `*_content.py` files and fails when one declares nothing, so the gap surfaces in CI rather than
# at the first converge on a machine.

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from base.host.system import logs_content, packages_content, pr_flow_content, walg_content
from base.host.system.cron.heartbeat import (
    render_declaration,
    validate_exception,
    validate_population,
)
from base.host.system.job_values import (
    DIRECTORY_PARTS,
    SCHEMA,
    HeartbeatDeclaration,
    HeartbeatRender,
    JobHeartbeat,
    JobRoots,
    JobSchedule,
    MissingHeartbeatDeclaration,
    NoHeartbeat,
    RenderContext,
    heartbeat_path,
    launchd_domain,
)

__all__ = [
    "POPULATION",
    "MachinePolicy",
    "declarations_document",
    "verified",
]


# meiva:note
# One row per generated job: the label it is registered under and the declaration beside its
# trigger. `None` is a job that declares nothing — it renders nowhere, and the refusal names it.
POPULATION: tuple[tuple[str, HeartbeatDeclaration | None], ...] = (
    (logs_content.LABEL, logs_content.HEARTBEAT),
    (packages_content.LABEL, packages_content.HEARTBEAT),
    (pr_flow_content.LABEL, pr_flow_content.HEARTBEAT),
    (walg_content.LABEL, walg_content.HEARTBEAT),
)


# meiva:note
# The configuration that decides which of the four this machine should be running. The caller
# reads it once and hands it in as values, so "does this machine expect this job" is answerable
# without a converge.
@dataclass(frozen=True, kw_only=True)
class MachinePolicy:
    jobs_enabled: bool
    owned_home: bool
    packages_refresh: bool
    packages_tick_seconds: int | None
    walg_configured: bool
    production: bool
    backup_hour: int


def _declaration_of(job: str) -> HeartbeatDeclaration | None:
    for label, declaration in POPULATION:
        if label == job:
            return declaration
    return None


# meiva:note
# Validate this job's declaration against the whole population and render its product.
def verified(
    job: str,
    *,
    roots: JobRoots,
    machine: str,
    uid: int,
    schedule: JobSchedule,
    today: date,
) -> HeartbeatRender:
    if all(label != job for label, _ in POPULATION):
        raise MissingHeartbeatDeclaration(
            f"{job} is not in the declared population: a generated job listed nowhere has no "
            f"expectation set to be judged against, which is the silence this list ends"
        )
    return render_declaration(
        job=job,
        declaration=_declaration_of(job),
        population=POPULATION,
        schedule=schedule,
        context=RenderContext(home=roots.home, machine=machine, uid=uid, today=today),
    )


# meiva:note
# Which jobs this machine's configuration asks for. The registrars gate themselves with the same
# switches; the difference is that a registrar logs and returns while this returns a value, so
# the two must be read together when a gate changes.
def expected_on_this_machine(policy: MachinePolicy, job: str) -> bool:
    if not (policy.jobs_enabled and policy.owned_home):
        return False
    if job == packages_content.LABEL:
        return policy.packages_refresh
    if job == pr_flow_content.LABEL:
        return policy.production
    if job == walg_content.LABEL:
        return policy.walg_configured
    return job == logs_content.LABEL


def _schedule_of(policy: MachinePolicy, job: str) -> JobSchedule | None:
    if job == packages_content.LABEL:
        tick = policy.packages_tick_seconds
        return None if tick is None else packages_content.schedule(tick_seconds=tick)
    if job == walg_content.LABEL:
        return walg_content.schedule(backup_hour=policy.backup_hour)
    if job == pr_flow_content.LABEL:
        return pr_flow_content.schedule()
    return logs_content.schedule()


def _entry(
    label: str,
    declaration: HeartbeatDeclaration | None,
    *,
    policy: MachinePolicy,
    roots: JobRoots,
    domain: str,
    today: date,
) -> dict[str, object]:
    expected = expected_on_this_machine(policy, label)
    if isinstance(declaration, NoHeartbeat):
        validate_exception(declaration, job=label)
        until = declaration.until
        return {
            "label": label,
            "heartbeat_name": None,
            "installed_expected": expected,
            "owner": declaration.owner,
            "exception": {
                "reason": declaration.reason,
                "until": None if until is None else until.isoformat(),
                "expired": until is not None and until < today,
            },
        }
    if not isinstance(declaration, JobHeartbeat):
        return {"label": label, "heartbeat_name": None, "installed_expected": expected}
    schedule = _schedule_of(policy, label)
    return {
        "label": label,
        "heartbeat_name": declaration.name,
        "heartbeat_dir": str(roots.home.joinpath(*DIRECTORY_PARTS)),
        "heartbeat_file": str(heartbeat_path(roots.home, declaration.name)),
        "interval_seconds": None if schedule is None else schedule.interval_seconds(),
        "late_after_seconds": declaration.late_after_seconds,
        "owner": declaration.owner,
        "domain": domain,
        "installed_expected": expected,
    }


# meiva:note
# The expectation set of this machine as data — what a reader outside this checkout diffs the
# scheduler against. `installed_expected` carries the configuration gates, so a job this machine
# deliberately does not run is not reported as missing.
def declarations_document(
    *,
    machine: str,
    repo_sha: str | None,
    roots: JobRoots,
    uid: int,
    policy: MachinePolicy,
    today: date,
) -> dict[str, object]:
    validate_population(POPULATION)
    domain = launchd_domain(uid)
    return {
        "schema": SCHEMA,
        "machine": machine,
        "repo_sha": repo_sha,
        "home": str(roots.home),
        "jobs": [
            _entry(
                label,
                declaration,
                policy=policy,
                roots=roots,
                domain=domain,
                today=today,
            )
            for label, declaration in POPULATION
        ],
    }
