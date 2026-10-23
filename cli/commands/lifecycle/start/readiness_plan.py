# meiva:note
# What the start reports and exits with once the launched services had their readiness wait.
#
# Exit zero means the selected core services are ready. Optional capabilities can stay
# unavailable without preventing the host from serving: they are reported and signalled,
# never a reason to fail. The shell executes the verdict returned here, so the exit code and
# the printed snapshot describe the same run.

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from base.host.exit_codes import SERVICES_NOT_READY_EXIT_CODE

__all__ = [
    "ReadinessFacts",
    "ReadinessVerdict",
    "archive_assertion_needed",
    "critical_launch_failures",
    "readiness_verdict",
    "shows_gateway_hint",
]


# meiva:note
# What one launch and its readiness wait found, as plain values.
#
# `launch_failed` and `critical` hold session display names; `unready` and
# `non_critical_unready` say whether any core or optional service is not ready.
@dataclass(frozen=True)
class ReadinessFacts:
    launch_failed: tuple[str, ...]
    critical: frozenset[str]
    unready: bool
    non_critical_unready: bool


# meiva:note
# The report of a start's readiness and the exit code it ends with (None: carry on).
@dataclass(frozen=True)
class ReadinessVerdict:
    launch_failure_line: str | None
    critical_launch_failures: tuple[str, ...]
    report_unready: bool
    report_non_critical_unready: bool
    exit_code: int | None


# meiva:note
# The launch failures among the services core to serving.
def critical_launch_failures(
    launch_failed: tuple[str, ...], critical: Collection[str]
) -> tuple[str, ...]:
    return tuple(name for name in launch_failed if name in critical)


# meiva:note
# Report every failure, but gate serving only on the selected core services.
def readiness_verdict(facts: ReadinessFacts) -> ReadinessVerdict:
    critical = critical_launch_failures(facts.launch_failed, facts.critical)
    failure_line = None
    if facts.launch_failed:
        failure_line = (
            f"\n✗ {len(facts.launch_failed)} service(s) could not be launched "
            f": {', '.join(facts.launch_failed)}"
        )
    blocked = facts.unready or bool(critical)
    return ReadinessVerdict(
        launch_failure_line=failure_line,
        critical_launch_failures=critical,
        report_unready=facts.unready,
        report_non_critical_unready=facts.non_critical_unready,
        exit_code=SERVICES_NOT_READY_EXIT_CODE if blocked else None,
    )


# meiva:note
# Whether a start that is serving asserts its Postgres carries the archive settings.
#
# The gate is the machine's role, not what this start launched, so a start against an
# already-running tree still checks; a gateway among the unready units skips it, because
# its Postgres is not known to be serving.
def archive_assertion_needed(roles: Collection[str], *, gateway_unready: bool) -> bool:
    return "gateway" in roles and not gateway_unready


# meiva:note
# Whether the start tells how to reach a gateway it launched: only a ready one.
def shows_gateway_hint(launched: Collection[str], *, unready: bool) -> bool:
    return "gateway" in launched and not unready
