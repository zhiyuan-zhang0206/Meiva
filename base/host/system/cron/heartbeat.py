# meiva:note
# Turn a job's declared heartbeat into the artifact: the environment entry the definition carries
# and the one line that states what this machine now expects, refusing the shapes that would leave
# the promise unreadable.
#
# Two refusals keep it from decaying into a review checklist:
#
# - A job that declares nothing (`None`) does not render. The only way out is the explicit
#   exception form, which carries a reason, an owner and an `until`.
# - Two jobs that declare the same heartbeat name on one machine do not render. One file would
#   answer for two jobs, so a dead job would keep a live one's heartbeat fresh.

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from base.host.system.job_values import (
    EXCEPTION_DECLARED,
    EXPIRING_WITHIN_DAYS,
    HEARTBEAT_ENV,
    SCHEMA,
    DuplicateHeartbeatName,
    HeartbeatDeclaration,
    HeartbeatRender,
    InvalidHeartbeatDeclaration,
    JobHeartbeat,
    JobSchedule,
    MissingHeartbeatDeclaration,
    NoHeartbeat,
    RenderContext,
    heartbeat_path,
    launchd_domain,
    one_line_without_comment_end,
)

__all__ = [
    "exit_document",
    "render_declaration",
    "validate_exception",
    "validate_population",
]


# meiva:note
# Refuse two declarations that name one heartbeat file on one machine, naming both jobs.
def validate_population(population: Sequence[tuple[str, HeartbeatDeclaration | None]]) -> None:
    seen: dict[str, str] = {}
    for job, declaration in population:
        if not isinstance(declaration, JobHeartbeat):
            continue
        first = seen.setdefault(declaration.name, job)
        if first != job:
            raise DuplicateHeartbeatName(
                f"{first} and {job} both declare the heartbeat name {declaration.name!r} on one "
                f"machine: one file would answer for both jobs, so a dead job would keep the "
                f"other's heartbeat fresh"
            )


# meiva:note
# An exception is only as good as its three parts: what it stands for, who picks it up, and when
# it stops being an exception. Any of them missing is refused here rather than accepted as a
# declaration, because the shape that reaches nobody is silently the same as no heartbeat at all.
def validate_exception(declaration: NoHeartbeat, *, job: str) -> None:
    missing = [
        field
        for field, present in (
            ("reason", bool(declaration.reason.strip())),
            ("owner", bool(declaration.owner.strip())),
            ("until", declaration.until is not None),
        )
        if not present
    ]
    if missing:
        raise InvalidHeartbeatDeclaration(
            f"{job} declares no heartbeat but is missing {', '.join(missing)}: an exception "
            f"without a reason, an owner and an end date is a job no one will ever pick up"
        )
    for field, value in (("reason", declaration.reason), ("owner", declaration.owner)):
        _ = one_line_without_comment_end(value, field=field)


def _tiers(
    population: Sequence[tuple[str, HeartbeatDeclaration | None]], *, today: date
) -> tuple[int, int, int]:
    heartbeat = sum(1 for _, declared in population if isinstance(declared, JobHeartbeat))
    live = 0
    expired = 0
    for _, declared in population:
        if isinstance(declared, NoHeartbeat):
            if declared.until is not None and declared.until < today:
                expired += 1
            else:
                live += 1
    return heartbeat, live, expired


def _counts(population: Sequence[tuple[str, HeartbeatDeclaration | None]], *, today: date) -> str:
    heartbeat, live, expired = _tiers(population, today=today)
    return f"declared={len(population)} heartbeat={heartbeat} none={live} expired={expired}"


def _exception_line(declaration: NoHeartbeat, *, job: str, today: date) -> str:
    until = declaration.until
    if until is None:
        raise InvalidHeartbeatDeclaration(f"{job} declares no heartbeat without an until")
    left = (until - today).days
    if until < today:
        state = f"expired:{job}"
    elif left <= EXPIRING_WITHIN_DAYS:
        state = f"none-expiring:{job} in {left} days"
    else:
        state = EXCEPTION_DECLARED
    return (
        f"{state} job={job} owner={declaration.owner} "
        f"until={until.isoformat()} reason={declaration.reason}"
    )


def _declaration_lines(
    *,
    declaration: HeartbeatDeclaration,
    job: str,
    home: Path,
    machine: str,
    domain: str,
    schedule: JobSchedule,
) -> tuple[str, tuple[tuple[str, str], ...], Path | None]:
    if isinstance(declaration, NoHeartbeat):
        line = (
            f"job={job} owner={declaration.owner} until={declaration.until} "
            f"reason={declaration.reason} domain={domain} machine={machine} "
            f"schedule={schedule.described()}"
        )
        return line, (), None
    path = heartbeat_path(home, declaration.name)
    line = (
        f"job={job} name={declaration.name} dir={path.parent} "
        f"late_after={declaration.late_after_seconds} domain={domain} owner={declaration.owner} "
        f"machine={machine} schedule={schedule.described()}"
    )
    return line, ((HEARTBEAT_ENV, str(path)),), path


# meiva:note
# Render one job's heartbeat product, refusing a job that declares nothing and an exception that
# is missing any of its three parts.
def render_declaration(
    *,
    job: str,
    declaration: HeartbeatDeclaration | None,
    population: Sequence[tuple[str, HeartbeatDeclaration | None]],
    schedule: JobSchedule,
    context: RenderContext,
) -> HeartbeatRender:
    if declaration is None:
        raise MissingHeartbeatDeclaration(
            f"{job} renders an OS job definition but declares no heartbeat: declare a "
            f"JobHeartbeat beside its trigger, or the exception form carrying a reason, an "
            f"owner and an until"
        )
    if not any(candidate == job for candidate, _ in population):
        raise MissingHeartbeatDeclaration(
            f"{job} is not in the declared population: a job listed nowhere has no expectation "
            f"set to be judged against, which is the silence this declaration exists to end"
        )
    validate_population(population)
    if isinstance(declaration, NoHeartbeat):
        validate_exception(declaration, job=job)
    domain = launchd_domain(context.uid)
    body, environment, path = _declaration_lines(
        declaration=declaration,
        job=job,
        home=context.home,
        machine=context.machine,
        domain=domain,
        schedule=schedule,
    )
    if isinstance(declaration, NoHeartbeat):
        body = f"{_exception_line(declaration, job=job, today=context.today)} {body}"
    artifact = f"{_counts(population, today=context.today)} {body}"
    return HeartbeatRender(job=job, file=path, environment=environment, artifact=artifact)


# meiva:note
# The JSON a finished job writes: it ran, this is what it exited with, at this moment. The field
# names are the reader's (schema/name/pid/updated_at/ok/rc); `updated_at` is epoch seconds and
# must be the end instant, because a reader refuses a value more than thirty seconds ahead of it.
def exit_document(*, name: str, pid: int, updated_at: float, rc: int) -> str:
    if not name.strip():
        raise ValueError("a heartbeat needs the declared name as its file stem")
    if pid <= 0:
        raise ValueError(f"a heartbeat records the run's own pid, got {pid!r}")
    if not math.isfinite(updated_at) or updated_at <= 0:
        raise ValueError(
            f"a heartbeat records the end instant in epoch seconds, got {updated_at!r}"
        )
    document: Mapping[str, object] = {
        "schema": SCHEMA,
        "name": name,
        "pid": pid,
        "updated_at": round(updated_at, 3),
        "ok": rc == 0,
        "rc": rc,
    }
    return json.dumps(document, sort_keys=False)
