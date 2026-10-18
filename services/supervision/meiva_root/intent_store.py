"""Durable per-unit intent records: the policy fact a restarted root re-derives.

One JSON file per unit under `<run_dir>/intent/<unit>.json` records whether the
unit is meant to run, who last said so, and the explicit failure state of an
interrupted replacement. Mechanical transition state is deliberately absent:
only explicit stops, explicit starts, and recorded failures are stored, so a
failed self-rescue can never read back as an operator action after a restart
(task #4872).

Format (times are epoch seconds):

    {"intent": "running"|"stopped",
     "source": "operator"|"self"|"selection",
     "restart_failed": {"stage": "down"|"up", "since": <epoch>, "detail": str} | null,
     "updated_at": <epoch>}

`write` is best-effort by design: an unwritable record must never fail an
operator verb whose process work already happened — the loss is logged loudly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import time
from typing import cast

_log = logging.getLogger(__name__)

_DIRECTORY = "intent"
_RECORD_FIELDS = frozenset({"intent", "source", "restart_failed", "updated_at", "caller"})
_LEGACY_RECORD_FIELDS = frozenset({"intent", "source", "restart_failed", "updated_at"})
_CALLER_FIELDS = frozenset(
    {"pid", "ppid", "uid", "cwd", "argv", "agent_id", "machine", "operation_id", "env_keys"}
)
_FAILURE_FIELDS = frozenset({"stage", "since", "detail"})


class UnitIntent(StrEnum):
    """What the unit is meant to be doing — the only policy fact classification reads."""

    RUNNING = "running"
    STOPPED = "stopped"


class IntentSource(StrEnum):
    """Who last set a unit's intent."""

    OPERATOR = "operator"
    """An explicit operator verb (up / down / restart)."""
    SELF = "self"
    """The root's own lifecycle (boot start, whole-tree shutdown)."""
    SELECTION = "selection"
    """Derived from the admitted service selection."""


class RestartStage(StrEnum):
    """Which half of a replacement failed."""

    DOWN = "down"
    """The old generation could not be stopped; it may still be alive."""
    UP = "up"
    """The replacement generation is not confirmed active."""


@dataclass(frozen=True, slots=True)
class RestartFailure:
    """One explicitly recorded replacement failure; cleared only by success."""

    stage: RestartStage
    since: float
    detail: str


# meiva:note
# Who asked for the state change this record carries: pid/ppid/uid/cwd/argv name
# the requesting process locally, `machine` names its host, and `env_keys`
# carries variable *names* only. Persisted so the attribution outlives it.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
@dataclass(frozen=True, slots=True)
class CallerFacts:
    pid: int
    ppid: int
    uid: int
    cwd: str
    argv: tuple[str, ...]
    agent_id: str | None
    machine: str
    operation_id: str | None
    env_keys: tuple[str, ...]

    def summary(self) -> str:
        who = self.agent_id or "no-agent"
        return f"{who} pid={self.pid} uid={self.uid} on {self.machine} ({self.cwd})"


@dataclass(frozen=True, slots=True)
class IntentRecord:
    intent: UnitIntent
    source: IntentSource
    restart_failed: RestartFailure | None
    caller: CallerFacts | None = None


@dataclass(frozen=True, slots=True)
class BootIntent:
    """One unit's intent as the boot merge derives it from its stored record."""

    intent: UnitIntent
    source: IntentSource
    restart_failed: RestartFailure | None
    start: bool
    note: str | None


def merge_record_for_boot(record: IntentRecord | None) -> BootIntent:
    """The one boot rule for a stored record (conservative merge, task #4872 §7⑤).

    - no record (or an unreadable one — `read` has logged it): this admitted
      start is the only word; default to running/selection.
    - a recorded stop by the operator or the selection holds: boot never
      overrides an explicit stop; a later operator start supersedes it through
      the ordinary verbs.
    - a stop the root gave itself (shutdown) is mechanical, not policy: this
      start supersedes it with running/selection.
    - a recorded replacement failure is carried over until a fresh generation
      proves it gone — an unresolved failure is never cleared by a restart
      alone.
    """
    if record is None:
        return BootIntent(
            intent=UnitIntent.RUNNING,
            source=IntentSource.SELECTION,
            restart_failed=None,
            start=True,
            note=None,
        )
    if record.intent is UnitIntent.STOPPED:
        if record.source is IntentSource.SELF:
            return BootIntent(
                intent=UnitIntent.RUNNING,
                source=IntentSource.SELECTION,
                restart_failed=record.restart_failed,
                start=True,
                note="stored root stop superseded by this start",
            )
        return BootIntent(
            intent=UnitIntent.STOPPED,
            source=record.source,
            restart_failed=record.restart_failed,
            start=False,
            note=f"stored {record.source.value} stop held; this start leaves the unit stopped",
        )
    return BootIntent(
        intent=UnitIntent.RUNNING,
        source=record.source,
        restart_failed=record.restart_failed,
        start=True,
        note=None,
    )


def write(run_dir: Path, unit: str, record: IntentRecord) -> None:
    """Persist one unit's record atomically; failures are logged, never raised."""
    from base.host.atomic_io import write_text_atomic

    try:
        directory = run_dir / _DIRECTORY
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_text_atomic(directory / f"{unit}.json", _encode(record), mode=0o600, sync_parent=True)
    except OSError as exc:
        _log.error("unit %s: intent record write failed: %s", unit, exc)
        return
    _log.debug(
        "unit %s: intent record written (%s/%s)", unit, record.intent.value, record.source.value
    )


def read(run_dir: Path, unit: str) -> IntentRecord | None:
    """Read one unit's record; absent or unreadable yields None (logged)."""
    path = run_dir / _DIRECTORY / f"{unit}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.warning("unit %s: intent record unreadable (%s); boot default applies", unit, exc)
        return None
    try:
        return _decode(json.loads(text))
    except (ValueError, TypeError) as exc:
        _log.warning("unit %s: intent record invalid (%s); boot default applies", unit, exc)
        return None


def _encode(record: IntentRecord) -> str:
    failure = record.restart_failed
    body: dict[str, object] = {
        "intent": record.intent.value,
        "source": record.source.value,
        "restart_failed": None
        if failure is None
        else {"stage": failure.stage.value, "since": failure.since, "detail": failure.detail},
        "updated_at": time(),
    }
    if record.caller is not None:
        body["caller"] = _encode_caller(record.caller)
    return json.dumps(body, separators=(",", ":"), sort_keys=True) + "\n"


def _decode(raw: object) -> IntentRecord:
    if not isinstance(raw, dict):
        raise TypeError("record must be an object")
    values = cast("Mapping[str, object]", raw)
    fields = frozenset(values)
    if fields not in (_RECORD_FIELDS, _LEGACY_RECORD_FIELDS):
        raise ValueError(f"record must contain exactly {sorted(_RECORD_FIELDS)}")
    intent = _member(UnitIntent, values["intent"], "intent")
    source = _member(IntentSource, values["source"], "source")
    return IntentRecord(
        intent,
        source,
        _decode_failure(values["restart_failed"]),
        _decode_caller(values.get("caller")),
    )


def _encode_caller(caller: CallerFacts) -> dict[str, object]:
    return {
        "pid": caller.pid,
        "ppid": caller.ppid,
        "uid": caller.uid,
        "cwd": caller.cwd,
        "argv": list(caller.argv),
        "agent_id": caller.agent_id,
        "machine": caller.machine,
        "operation_id": caller.operation_id,
        "env_keys": list(caller.env_keys),
    }


# meiva:note
# A caller block written by an older root, or none at all, reads as absent:
# attribution is evidence, never a boot gate.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
def _decode_caller(raw: object) -> CallerFacts | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    values = cast("Mapping[str, object]", raw)
    if frozenset(values) != _CALLER_FIELDS:
        return None
    try:
        return CallerFacts(
            pid=int(cast("int", values["pid"])),
            ppid=int(cast("int", values["ppid"])),
            uid=int(cast("int", values["uid"])),
            cwd=str(values["cwd"]),
            argv=tuple(str(item) for item in cast("list[object]", values["argv"])),
            agent_id=None if values["agent_id"] is None else str(values["agent_id"]),
            machine=str(values["machine"]),
            operation_id=None if values["operation_id"] is None else str(values["operation_id"]),
            env_keys=tuple(str(item) for item in cast("list[object]", values["env_keys"])),
        )
    except (TypeError, ValueError):
        return None


def _decode_failure(raw: object) -> RestartFailure | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("restart_failed must be an object or null")
    values = cast("dict[str, object]", raw)
    if frozenset(values) != _FAILURE_FIELDS:
        raise ValueError(f"restart_failed must contain exactly {sorted(_FAILURE_FIELDS)}")
    stage = _member(RestartStage, values["stage"], "restart_failed.stage")
    since = values["since"]
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        raise TypeError("restart_failed.since must be a number")
    detail = values["detail"]
    if not isinstance(detail, str):
        raise TypeError("restart_failed.detail must be a string")
    return RestartFailure(stage, float(since), detail)


def _member[E: StrEnum](enum_type: type[E], raw: object, field: str) -> E:
    if not isinstance(raw, str):
        raise TypeError(f"{field} must be a string")
    try:
        return enum_type(raw)
    except ValueError as exc:
        choices = [item.value for item in enum_type]
        raise ValueError(f"{field} {raw!r} is not one of {choices}") from exc
