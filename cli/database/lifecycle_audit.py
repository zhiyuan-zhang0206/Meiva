# meiva:note
# The audit rows of an operator lifecycle command: what it asked the root for, how it ended.
#
# Imports that reach the configuration chain stay inside the functions that use them, so
# `meiva --help` can import this module without loading settings.

from __future__ import annotations

import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Literal

from base.agents.context.clients import DatabaseFactory
from base.log import logger
from base.native_process.root_control.ipc import CallerPayload

__all__ = [
    "AuditUnavailableError",
    "CommandAudit",
    "lifecycle_audit",
    "operator_lifecycle_audit",
    "operator_request_recorder",
    "operator_result_recorder",
    "record_lifecycle_request",
    "record_lifecycle_result",
]


# meiva:note
# The audit scope of a command cannot be opened because the process cannot name its caller or its
# source commit. `cli.main` reports it as one line and exits 1, before the command ran.
class AuditUnavailableError(RuntimeError): ...


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
# worth a row. The caller block and the source commit are read once, when the scope
# opens. A reading that fails is an environment fault (a working directory that is gone, a
# home that cannot be created) and refuses the command with an `AuditUnavailableError` naming it,
# before the command changes anything: a command must not run unattributed behind the
# operator's back. A source checkout with no readable commit is not a fault; its `head_sha`
# is None. Writing a row stays best-effort once the scope is open: the record must never
# fail the command that repairs the host.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
@contextmanager
def operator_lifecycle_audit(
    *,
    database_factory: DatabaseFactory,
    enabled: bool = True,
) -> Generator[CommandAudit, None, None]:
    if not enabled:
        with lifecycle_audit(None) as command:
            yield command
        return
    try:
        caller = _caller_identity()
    except OSError as exc:
        raise AuditUnavailableError(f"lifecycle audit cannot read the caller block: {exc}") from exc
    try:
        head_sha = _head_sha()
    except OSError as exc:
        raise AuditUnavailableError(
            f"lifecycle audit cannot read the source commit: {exc}"
        ) from exc
    with lifecycle_audit(
        operator_request_recorder(
            database_factory=database_factory, caller=caller, head_sha=head_sha
        ),
        operator_result_recorder(
            database_factory=database_factory, caller=caller, head_sha=head_sha
        ),
    ) as command:
        yield command


def _agent_id(raw: str | None) -> int | None:
    return int(raw) if raw is not None and raw.isdigit() else None


def _caller_identity() -> CallerPayload:
    from base.host.env.registry import process_environment
    from base.native_process.root_control.client import caller_identity

    return caller_identity(process_environment())


def _head_sha() -> str | None:
    from base.deploy.git.cluster_drift import prod_source_head_sha
    from base.host.env.registry import process_environment
    from base.paths import meiva_home

    return prod_source_head_sha(meiva_home(), environ=process_environment())


# meiva:note
# Record each request of the command that `caller` names, against `head_sha`.
def operator_request_recorder(
    *,
    database_factory: DatabaseFactory,
    caller: CallerPayload,
    head_sha: str | None,
) -> RequestRecorder:
    def recorder(verb: str, units: Sequence[str]) -> None:
        try:
            _record(
                "lifecycle_request",
                {"verb": verb, "units": list(units), "caller": dict(caller)},
                database_factory=database_factory,
                caller=caller,
                head_sha=head_sha,
            )
        except Exception:
            logger.opt(exception=True).error(
                "lifecycle request {verb} could not be recorded", verb=verb
            )

    return recorder


# meiva:note
# Record the outcome of the command that `caller` names, against `head_sha`.
def operator_result_recorder(
    *,
    database_factory: DatabaseFactory,
    caller: CallerPayload,
    head_sha: str | None,
) -> ResultRecorder:
    def recorder(verb: str, units: Sequence[str], outcome: Outcome, seconds: float) -> None:
        try:
            _record(
                "lifecycle_result",
                {
                    "verb": verb,
                    "units": list(units),
                    "outcome": outcome,
                    "seconds": round(seconds, 3),
                },
                database_factory=database_factory,
                caller=caller,
                head_sha=head_sha,
            )
        except Exception:
            logger.opt(exception=True).error(
                "lifecycle result {verb} could not be recorded", verb=verb
            )

    return recorder


def _record(
    event_type: str,
    fields: Mapping[str, object],
    *,
    database_factory: DatabaseFactory,
    caller: CallerPayload,
    head_sha: str | None,
) -> None:
    from base.telemetry.audit_events import prepare_event_log, record_audit_reported

    agent_id = _agent_id(caller["agent_id"])
    event = prepare_event_log(
        event_type=event_type,
        agent_id=agent_id,
        source=f"agent:{agent_id}" if agent_id is not None else "user",
        payload={**fields, "operation_id": caller["operation_id"], "head_sha": head_sha},
    )
    record_audit_reported(database_factory(), event)
