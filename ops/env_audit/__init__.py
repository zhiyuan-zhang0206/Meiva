# meiva:note
# Owner-only `.env` write history and out-of-band modification detection.
#
# The JSONL history records `.env` aliases, metadata, digests, the initiating credential fact
# and, for non-sensitive fields only, the old to new value diff: configuration values of
# sensitive (or unregistered) keys never enter this file, and configuration values never enter
# the unified event stream at all. First-start provisioning (`cli/start_identity.py`)
# intentionally creates a fresh `.env` without audit history or an armed marker, so the guard
# remains unarmed until an audited runtime write.
#
# This is the shell: it reads the registry, the process, the clock and the files, and executes
# what `redaction`, `write_record` and `integrity_plan` decide. Every function takes the `.env`
# path it audits; no function resolves the process home.

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard, assert_never

import psutil

from base.host.env.dotenv_content import env_line_key
from base.host.env.dotenv_file import ENV_LOCK_TIMEOUT_S, env_lock_path
from base.host.private_storage import write_private_bytes
from base.log import logger
from base.native_process.os_platform import file_lock
from ops.env_audit.integrity_plan import (
    EmptyHistory,
    History,
    Intact,
    NoHistory,
    Tampered,
    UnreadableHistory,
    is_armed,
    plan_integrity,
    recorded_history,
)
from ops.env_audit.redaction import (
    AliasMeta,
    FieldDeclaration,
    RedactedChange,
    alias_metadata,
    process_label,
    redact_changes,
)
from ops.env_audit.write_record import WriteFacts, write_event, write_record

__all__ = [
    "EnvWriteAudit",
    "check_env_integrity",
    "env_key_names",
    "load_alias_metadata",
    "prepare_env_changes",
    "read_env_write_records",
    "record_env_write",
]

_AUDIT_TAIL_BYTES = 64 * 1024


def _audit_path(env_path: Path) -> Path:
    return env_path.with_name(".env.audit.jsonl")


def _armed_path(env_path: Path) -> Path:
    return env_path.with_name(".env.audit.armed")


# meiva:note
# Present key names, without retaining or emitting their values.
def env_key_names(env_path: Path) -> list[str]:
    if not env_path.exists():
        return []
    names: set[str] = set()
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        key = env_line_key(line)
        if key is not None:
            names.add(key)
    return sorted(names)


# meiva:note
# Project the field declarations of the config registry onto their `.env` aliases.
#
# Read live on every call: the registry is the only copy, so there is no image to go stale.
# Only declarations are read, never runtime values or panel metadata, so the projection works
# in a process that has no settings image yet.
def load_alias_metadata() -> dict[str, AliasMeta]:
    from base.config.config_registry import field_alias, fields, schema_extra

    declarations: list[FieldDeclaration] = []
    for name, ref in fields().items():
        extra = schema_extra(ref.info)
        declarations.append(
            FieldDeclaration(
                name=name,
                alias=field_alias(name),
                scope=extra["scope"],
                sensitive=extra.get("sensitive", False),
            )
        )
    return alias_metadata(declarations)


# meiva:note
# Declaration facts for redaction. Invalid metadata is reported and raised, so it prevents
# audit preparation instead of degrading into a names-only record.
def _alias_metadata() -> dict[str, AliasMeta]:
    try:
        return load_alias_metadata()
    except Exception:
        logger.bind(_no_emitter=True).opt(exception=True).error(
            "could not load declared config metadata for the .env audit"
        )
        raise


# meiva:note
# Audit-owned redacted values prepared before the caller changes its file.
@dataclass(frozen=True)
class _PreparedEnvChanges:
    changes: tuple[RedactedChange, ...]


# meiva:note
# Resolve declarations and redact a diff while the writer still holds the old bytes.
def prepare_env_changes(changes: Sequence[Mapping[str, object]]) -> _PreparedEnvChanges:
    return _PreparedEnvChanges(redact_changes(changes, _alias_metadata()))


# meiva:note
# The current process name and a safe, bounded command line (executable only).
def _process_metadata() -> tuple[str, str]:
    try:
        process = psutil.Process()
        return process_label(str(process.name()), [str(part) for part in process.cmdline()])
    except (psutil.Error, OSError):
        return Path(sys.argv[0]).name, ""


def _append_record(audit_path: Path, record: Mapping[str, object]) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


# meiva:note
# Create or repair the owner-only marker before writing audit history, so a crash can leave an
# armed repair event but never a history whose deletion recreates the fresh-home branch.
def _write_armed_marker(env_path: Path) -> None:
    armed_path = _armed_path(env_path)
    armed_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(armed_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


# meiva:note
# Send a best-effort, value-free audit event to the unified event stream.
#
# The record of a `.env` write is this module's per-home JSONL: its writers run where no
# database identity exists, so the event is a projection and is deliberately not recorded in
# `audit_events`.
# meiva:link docs/decisions/runtime/config/2026-10-02-env-write-audit-stays-local.md
def _emit_audit_event(event_type: str, payload: dict[str, object]) -> None:
    from base import telemetry
    from base.telemetry.audit_events import prepare_event_log

    telemetry.emit_prepared(
        prepare_event_log(event_type=event_type, agent_id=None, source="system", payload=payload)
    )


def _digest(env_path: Path) -> str:
    return hashlib.sha256(env_path.read_bytes() if env_path.exists() else b"").hexdigest()


# meiva:note
# Append metadata for an official `.env` write that has already landed.
#
# `keys_written` and `keys_removed` use the `.env` alias vocabulary. Callers invoke this while
# holding `base.host.env.dotenv_file.env_lock_path`'s lock, so the recorded digest describes
# exactly the bytes their write completed. `changes` is the raw `{alias, old, new}` diff the
# caller captured before its rewrite, or the result of `prepare_env_changes` before its rewrite.
# Direct raw diffs are redacted here; prepared diffs never reload metadata after the write.
# Only non-sensitive registered fields retain values. `actor` names the initiating credential
# fact (`user_session:administrator`, `cluster_bearer:administrator`, `cli:zzy`) and `trace_id`
# the gateway request, when the write had one.
def record_env_write(
    env_path: Path,
    keys_written: set[str],
    keys_removed: set[str],
    *,
    site: str,
    actor: str | None = None,
    trace_id: str | None = None,
    changes: Sequence[Mapping[str, object]] | _PreparedEnvChanges | None = None,
) -> None:
    process, cmdline = _process_metadata()
    facts = WriteFacts(
        ts=datetime.now(UTC).isoformat(),
        site=site,
        pid=os.getpid(),
        process=process,
        cmdline=cmdline,
        actor=actor,
        trace_id=trace_id,
        keys_written=frozenset(keys_written),
        keys_removed=frozenset(keys_removed),
        keys_after=tuple(env_key_names(env_path)),
        digest_after=_digest(env_path),
    )
    if isinstance(changes, _PreparedEnvChanges):
        changed = changes.changes
    elif changes is not None:
        changed = redact_changes(changes, _alias_metadata())
    else:
        changed = None
    _write_armed_marker(env_path)
    _append_record(_audit_path(env_path), write_record(facts, changed))
    _emit_audit_event("env_write", write_event(facts))


# meiva:note
# The `EnvAudit` a writer passes to record one official `.env` write.
#
# `site` names the write path, `actor` the initiating credential fact and `trace_id` the
# gateway request, when the write had one.
@dataclass(frozen=True)
class EnvWriteAudit:
    site: str
    actor: str | None = None
    trace_id: str | None = None

    def prepare(self, changes: Sequence[Mapping[str, object]]) -> object:
        return prepare_env_changes(changes)

    def record(self, path: Path, written: set[str], removed: set[str], prepared: object) -> None:
        if not isinstance(prepared, _PreparedEnvChanges):
            raise TypeError("EnvWriteAudit.record needs the result of its own prepare")
        record_env_write(
            path,
            written,
            removed,
            site=self.site,
            actor=self.actor,
            trace_id=self.trace_id,
            changes=prepared,
        )


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


# meiva:note
# Read only the final JSONL line: the config-read hot path never loads the whole history.
def _last_audit_line(audit_path: Path) -> str | None:
    with audit_path.open("rb") as history:
        history.seek(0, os.SEEK_END)
        offset = max(0, history.tell() - _AUDIT_TAIL_BYTES)
        history.seek(offset)
        tail = history.read()
    line = tail.rstrip(b"\r\n").rsplit(b"\n", maxsplit=1)[-1]
    return line.decode("utf-8") if line else None


# meiva:note
# The newest `limit` audit records for `env_path`, newest first.
#
# Reads a bounded tail window (a record is far smaller than `_AUDIT_TAIL_BYTES`), so cost is
# independent of history size. A truncated first line (the window cut a record) is skipped by
# the same per-line tolerance that drops any corrupt line; a missing history returns [] (nothing
# has been written yet).
def read_env_write_records(limit: int, env_path: Path) -> list[dict[str, object]]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    audit_path = _audit_path(env_path)
    if not audit_path.exists():
        return []
    records: list[dict[str, object]] = []
    corrupt = 0
    text = audit_path.read_bytes()[-_AUDIT_TAIL_BYTES:].decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        if not _is_str_dict(record):
            corrupt += 1
            continue
        records.append(record)
        if len(records) >= limit:
            break
    if corrupt:
        logger.warning(
            "the env-write audit history {path} has {n} unreadable record(s) in the tail "
            "window; the listing is incomplete",
            path=str(audit_path),
            n=corrupt,
            event="env_audit_history",
        )
    return records


# meiva:note
# What the history file holds, as the plan's facts. A last line that is not a JSON object, or
# cannot be read at all (`ValueError` covers bad UTF-8 and bad JSON), is an unreadable history.
def _read_history(audit_path: Path) -> History:
    if not audit_path.exists():
        return NoHistory()
    try:
        line = _last_audit_line(audit_path)
        if line is None:
            return EmptyHistory()
        record = json.loads(line)
    except (OSError, ValueError):
        return UnreadableHistory()
    if not _is_str_dict(record):
        return UnreadableHistory()
    return recorded_history(record)


# meiva:note
# Persist the anomaly baseline and return the record. A damaged history is no trustworthy append
# target, so the plan asks for it to be replaced first.
def _record_tampering(env_path: Path, tampered: Tampered) -> dict[str, object]:
    audit_path = _audit_path(env_path)
    if tampered.mark_armed:
        _write_armed_marker(env_path)
    detection = tampered.record(datetime.now(UTC).isoformat())
    if tampered.rebuild_history:
        write_private_bytes(audit_path, b"")
    _append_record(audit_path, detection)
    return detection


# meiva:note
# Return an anomaly record when an armed audit digest no longer matches the `.env`.
#
# The guard is deliberately inert until an official write creates the audit history. Errors are
# contained because config reads must not fail merely because their anomaly detector cannot
# inspect a damaged file. `machine_name` labels the emitted event only; the unified event stream
# stamps its own machine field regardless.
def check_env_integrity(*, env_path: Path, machine_name: str) -> dict[str, object] | None:
    try:
        audit_path = _audit_path(env_path)
        marker = _armed_path(env_path).exists()
        with file_lock(env_lock_path(env_path), timeout_s=ENV_LOCK_TIMEOUT_S):
            history = _read_history(audit_path)
            if not is_armed(marker=marker, history=history):
                return None
            plan = plan_integrity(
                marker=marker,
                history=history,
                current_digest=_digest(env_path),
                current_keys=frozenset(env_key_names(env_path)),
            )
            match plan:
                case Intact(mark_armed=mark_armed):
                    if mark_armed:
                        _write_armed_marker(env_path)
                    return None
                case Tampered():
                    detection = _record_tampering(env_path, plan)
                case _:
                    assert_never(plan)
        _emit_audit_event("env_unauthorized_write", plan.event(machine_name))
        logger.error("{}", plan.summary())
        return detection
    except Exception:
        logger.opt(exception=True).warning("could not inspect .env audit integrity")
        return None
