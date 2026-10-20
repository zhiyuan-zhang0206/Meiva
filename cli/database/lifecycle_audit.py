from __future__ import annotations

import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Literal

from base.log import logger

__all__ = [
    "CommandAudit",
    "lifecycle_audit",
    "operator_lifecycle_audit",
    "operator_request_recorder",
    "operator_result_recorder",
    "record_lifecycle_request",
    "record_lifecycle_result",
]

RequestRecorder = Callable[[str, Sequence[str]], None]
Outcome = Literal["ok", "failed"]
ResultRecorder = Callable[[str, Sequence[str], Outcome, float], None]


# meiva:note
# One operator command's audit state: when it started, the last thing it asked for and
# whether it already recorded its end. The outcome is recorded once, from the command's
# own return path, so a command that dies mid-flight leaves its request without a result
# — which is exactly the shape "stopped and nothing came back" has (task #5464).
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
class CommandAudit:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.finished = False
        self.request: tuple[str, list[str]] | None = None

    def exited(self, code: int) -> None:
        if self.finished:
            raise RuntimeError("lifecycle command outcome recorded twice")
        self.finished = True
        record_lifecycle_result(
            outcome="ok" if code == 0 else "failed",
            seconds=time.monotonic() - self.started,
        )


class _Scope:
    recorder: RequestRecorder | None = None
    result: ResultRecorder | None = None
    command: CommandAudit | None = None


# meiva:note
# The recorder of the operator command this process is serving: what it is about
# to ask meiva-root to change, recorded before the first request leaves, plus the
# outcome recorded after the last one settles. The request is what makes a stop
# attributable even when the process dies; the result is what separates "stopped
# and never came back" from "stopped, restarted, same code" without reading logs
# (task #5464). It is scoped to that command because the recorder owns the
# command's database, and a nested command (a restart's start leg) restores what
# it found on exit.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
_scope = _Scope()


@contextmanager
def lifecycle_audit(
    recorder: RequestRecorder | None,
    result: ResultRecorder | None = None,
) -> Generator[CommandAudit, None, None]:
    previous = (_scope.recorder, _scope.result, _scope.command)
    command = CommandAudit()
    _scope.recorder, _scope.result, _scope.command = recorder, result, command
    try:
        yield command
    finally:
        _scope.recorder, _scope.result, _scope.command = previous


def record_lifecycle_request(verb: str, units: Sequence[str]) -> None:
    if _scope.command is not None:
        _scope.command.request = (verb, list(units))
    if _scope.recorder is not None:
        _scope.recorder(verb, units)


def record_lifecycle_result(*, outcome: Outcome, seconds: float) -> None:
    command = _scope.command
    if _scope.result is None or command is None or command.request is None:
        return
    verb, units = command.request
    _scope.result(verb, units, outcome, seconds)


# meiva:note
# The audit scope of one operator command: its request and its outcome, both written
# through the command's own database. `enabled=False` is the OS boot job's retried
# start — nothing asked for it, so there is no operator to attribute and no result
# worth a row.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
@contextmanager
def operator_lifecycle_audit(
    *,
    database_factory: Callable[[], Any],
    enabled: bool = True,
) -> Generator[CommandAudit, None, None]:
    if not enabled:
        with lifecycle_audit(None) as command:
            yield command
        return
    with lifecycle_audit(
        operator_request_recorder(database_factory=database_factory),
        operator_result_recorder(database_factory=database_factory),
    ) as command:
        yield command


def _agent_id(raw: str | None) -> int | None:
    return int(raw) if raw is not None and raw.isdigit() else None


def _caller_identity() -> Mapping[str, Any]:
    from base.native_process.root_control.client import caller_identity

    return caller_identity()


def _head_sha() -> str | None:
    from base.deploy.git.cluster_drift import prod_source_head_sha

    return prod_source_head_sha()


def _record_audit_reported(database: Any, event: Any) -> None:
    from base.telemetry.audit_events import record_audit_reported

    record_audit_reported(database, event)


def operator_request_recorder(
    *,
    database_factory: Callable[[], Any],
    caller_identity: Callable[[], Mapping[str, Any]] = _caller_identity,
    record_audit_reported: Callable[[Any, Any], None] = _record_audit_reported,
    head_sha: Callable[[], str | None] = _head_sha,
) -> RequestRecorder:
    def recorder(verb: str, units: Sequence[str]) -> None:
        try:
            _record(verb, units, database_factory, caller_identity, record_audit_reported, head_sha)
        except Exception:
            logger.opt(exception=True).error(
                "lifecycle request {verb} could not be recorded", verb=verb
            )

    return recorder


def operator_result_recorder(
    *,
    database_factory: Callable[[], Any],
    caller_identity: Callable[[], Mapping[str, Any]] = _caller_identity,
    record_audit_reported: Callable[[Any, Any], None] = _record_audit_reported,
    head_sha: Callable[[], str | None] = _head_sha,
) -> ResultRecorder:
    def recorder(verb: str, units: Sequence[str], outcome: Outcome, seconds: float) -> None:
        try:
            _record_result(
                verb,
                units,
                outcome,
                seconds,
                database_factory,
                caller_identity,
                record_audit_reported,
                head_sha,
            )
        except Exception:
            logger.opt(exception=True).error(
                "lifecycle result {verb} could not be recorded", verb=verb
            )

    return recorder


def _record(
    verb: str,
    units: Sequence[str],
    database_factory: Callable[[], Any],
    caller_identity: Callable[[], Mapping[str, Any]],
    record_audit_reported: Callable[[Any, Any], None],
    head_sha: Callable[[], str | None],
) -> None:
    from base.telemetry.audit_events import prepare_event_log

    caller = caller_identity()
    agent_id = _agent_id(caller["agent_id"])
    event = prepare_event_log(
        event_type="lifecycle_request",
        agent_id=agent_id,
        source=f"agent:{agent_id}" if agent_id is not None else "user",
        payload={
            "verb": verb,
            "units": list(units),
            "operation_id": caller["operation_id"],
            "caller": dict(caller),
            "head_sha": head_sha(),
        },
    )
    record_audit_reported(database_factory(), event)


def _record_result(
    verb: str,
    units: Sequence[str],
    outcome: Outcome,
    seconds: float,
    database_factory: Callable[[], Any],
    caller_identity: Callable[[], Mapping[str, Any]],
    record_audit_reported: Callable[[Any, Any], None],
    head_sha: Callable[[], str | None],
) -> None:
    from base.telemetry.audit_events import prepare_event_log

    caller = caller_identity()
    agent_id = _agent_id(caller["agent_id"])
    event = prepare_event_log(
        event_type="lifecycle_result",
        agent_id=agent_id,
        source=f"agent:{agent_id}" if agent_id is not None else "user",
        payload={
            "verb": verb,
            "units": list(units),
            "outcome": outcome,
            "seconds": round(seconds, 3),
            "operation_id": caller["operation_id"],
            "head_sha": head_sha(),
        },
    )
    record_audit_reported(database_factory(), event)
