from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from base.log import logger

__all__ = ["lifecycle_audit", "operator_request_recorder", "record_lifecycle_request"]

RequestRecorder = Callable[[str, Sequence[str]], None]


class _Scope:
    recorder: RequestRecorder | None = None


# meiva:note
# The recorder of the operator command this process is serving: what it is about
# to ask meiva-root to change, recorded before the first request leaves. It is
# scoped to that command because the recorder owns the command's database, and a
# nested command (a restart's start leg) restores what it found on exit.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
_scope = _Scope()


@contextmanager
def lifecycle_audit(recorder: RequestRecorder | None) -> Generator[None, None, None]:
    previous = _scope.recorder
    _scope.recorder = recorder
    try:
        yield
    finally:
        _scope.recorder = previous


def record_lifecycle_request(verb: str, units: Sequence[str]) -> None:
    if _scope.recorder is not None:
        _scope.recorder(verb, units)


def _agent_id(raw: str | None) -> int | None:
    return int(raw) if raw is not None and raw.isdigit() else None


def _caller_identity() -> Mapping[str, Any]:
    from base.native_process.root_control.client import caller_identity

    return caller_identity()


def _record_audit_reported(database: Any, event: Any) -> None:
    from base.telemetry.audit_events import record_audit_reported

    record_audit_reported(database, event)


def operator_request_recorder(
    *,
    database_factory: Callable[[], Any],
    caller_identity: Callable[[], Mapping[str, Any]] = _caller_identity,
    record_audit_reported: Callable[[Any, Any], None] = _record_audit_reported,
) -> RequestRecorder:
    def recorder(verb: str, units: Sequence[str]) -> None:
        try:
            _record(verb, units, database_factory, caller_identity, record_audit_reported)
        except Exception:
            logger.opt(exception=True).error(
                "lifecycle request {verb} could not be recorded", verb=verb
            )

    return recorder


def _record(
    verb: str,
    units: Sequence[str],
    database_factory: Callable[[], Any],
    caller_identity: Callable[[], Mapping[str, Any]],
    record_audit_reported: Callable[[Any, Any], None],
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
        },
    )
    record_audit_reported(database_factory(), event)
