from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from base.host.boundary.fields import int_field, str_field
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.session import BackgroundItem, decode_items, encode_items

_REQUIRED_FIELDS = frozenset(
    {
        "agent_id",
        "session_id",
        "cwd",
        "model",
        "created_at",
        "updated_at",
        "harness",
        "generation",
        "background",
    }
)
_FIELDS = _REQUIRED_FIELDS | {"resumes"}
_PRE_FIELD_HARNESS = "meiva"
_SINGLE_SLOT = re.compile(r"\d+\.json")
_HARNESS_NAME = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    agent_id: int
    session_id: str
    cwd: str
    model: str
    created_at: str
    updated_at: str
    harness: str
    # meiva:note
    # The process generation the session last ran under. Every event carries the generation of the
    # process that produced it, and a respawn or a resume is one more, so a record that outlives the
    # process keeps the count monotonic across sessions: a frame from an earlier process can never be
    # mistaken for one from the current process. Required: a record written before the field existed
    # is completed by `backfill_record_fields` in converge, never read leniently.
    # meiva:link base/lm/harness/docs/delivery.md
    generation: int
    resumes: int = 0
    # meiva:note
    # The background work the CLI held when this record was last written: what a restart would
    # lose. The session rewrites it whenever the set changes, so a host that dies without closing
    # the session still leaves it behind, and the next start reads it back as "lost" and tells the
    # agent once. It is a small list of one-line descriptions, not a log.
    # meiva:link base/lm/harness/docs/background-work.md
    background: tuple[BackgroundItem, ...] = ()


class SessionRegistry(Protocol):
    def load(self, agent_id: int, *, harness: str) -> SessionRecord | None: ...

    def save(self, record: SessionRecord) -> None: ...

    def delete(self, agent_id: int) -> bool: ...


# meiva:note
# The agent id is the durable identity and the SDK session id is the transcript it
# resumption needs, so the pair is written as soon as the SDK reports its init and
# refreshed on every turn. An agent keeps one record per harness it has used
# (`<agent_id>.<harness>.json`): a harness switch leaves the other harnesses' records
# alone, because the CLI's transcript is still on disk and a switch back resumes it.
# One document per (agent, harness) keeps writers apart, and each write is a temp
# file plus `os.replace` so a reader never sees a half-written record. A corrupt
# document raises instead of silently resuming a fresh session under an id that
# already has history. The layout before one record per harness was a single
# `<agent_id>.json`; `migrate_single_slot_records` converts those once, from host
# converge, and a file still in that layout makes every read refuse instead of
# resuming nothing. `delete` removes every record of the agent and is for moving the
# agent to another machine, never for a switch; `delete_harnesses` removes the records of the
# named harnesses, which a transfer uses for the sessions it carried to the other machine.
# meiva:link base/lm/harness/docs/harness.md
class FileSessionRegistry:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path(self, agent_id: int, harness: str) -> Path:
        _check_agent(agent_id)
        if _HARNESS_NAME.fullmatch(harness) is None:
            raise ValueError(f"harness name {harness!r} is not a lowercase identifier")
        return self._root / f"{agent_id}.{harness}.json"

    def _legacy_path(self, agent_id: int) -> Path:
        _check_agent(agent_id)
        return self._root / f"{agent_id}.json"

    def load(self, agent_id: int, *, harness: str) -> SessionRecord | None:
        self._refuse_single_slot(agent_id)
        return self._read(self.path(agent_id, harness))

    def records(self, agent_id: int) -> list[SessionRecord]:
        self._refuse_single_slot(agent_id)
        pattern = re.compile(rf"{agent_id}\.[a-z][a-z0-9_]*\.json")
        if not self._root.is_dir():
            return []
        found = [
            self._read(entry)
            for entry in sorted(self._root.iterdir())
            if pattern.fullmatch(entry.name) is not None
        ]
        return [record for record in found if record is not None]

    def _refuse_single_slot(self, agent_id: int) -> None:
        legacy = self._legacy_path(agent_id)
        if legacy.exists():
            raise ValueError(
                f"session record {legacy} is in the single-slot layout from before records were "
                "kept per harness; run `meiva converge` to migrate it"
            )

    def latest(self, agent_id: int) -> SessionRecord | None:
        return max(self.records(agent_id), key=lambda record: record.updated_at, default=None)

    @staticmethod
    def _read(path: Path) -> SessionRecord | None:
        try:
            raw = object_of(json.loads, path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError(f"session record at {path} is not valid JSON") from exc
        if not is_object_dict(raw):
            raise TypeError(f"session record at {path} is not an object")
        unknown = set(raw) - _FIELDS
        missing = _REQUIRED_FIELDS - set(raw)
        if unknown or missing:
            raise ValueError(
                f"session record at {path} has unknown fields {sorted(unknown)} "
                f"and misses {sorted(missing)}"
            )
        return record_from_payload(raw, source=str(path))

    def save(self, record: SessionRecord) -> None:
        path = self.path(record.agent_id, record.harness)
        path.parent.mkdir(parents=True, exist_ok=True)
        document: dict[str, object] = {
            **asdict(record),
            "background": encode_items(record.background),
        }
        _write_document(path, document)

    def delete_harnesses(self, agent_id: int, harnesses: Collection[str]) -> None:
        for harness in harnesses:
            self.path(agent_id, harness).unlink(missing_ok=True)
            self._root.joinpath(f"{agent_id}.{harness}.floor").unlink(missing_ok=True)
        TurnTimingStore(self._root).delete_harnesses(agent_id, harnesses)
        FileSessionDigestStore(self._root).delete_harnesses(agent_id, harnesses)

    def delete(self, agent_id: int) -> bool:
        _check_agent(agent_id)
        doomed = [self._legacy_path(agent_id)]
        if self._root.is_dir():
            pattern = re.compile(rf"{agent_id}\.[a-z][a-z0-9_]*\.(json|floor)")
            doomed += [entry for entry in self._root.iterdir() if pattern.fullmatch(entry.name)]
        removed = False
        for entry in doomed:
            try:
                entry.unlink()
            except FileNotFoundError:
                continue
            removed = True
        TurnTimingStore(self._root).delete(agent_id)
        FileSessionDigestStore(self._root).delete(agent_id)
        return removed


# meiva:note
# Convert every single-slot `<agent_id>.json` under `root` to `<agent_id>.<harness>.json`. A record
# without a harness field is from before adapters wrote one and was always the built-in `meiva`
# harness. Each record is written (temp file plus replace) before its old file is removed, so a run
# cut short leaves either the old file or both, and the next run finishes; an existing new-layout
# record is newer than any single-slot file and is kept. A file that is not a valid record raises
# naming the file: migrating around it would leave an agent that silently loses its session.
# Returns the migrated old files.
# meiva:link base/lm/harness/docs/harness.md
def migrate_single_slot_records(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    registry = FileSessionRegistry(root)
    migrated: list[Path] = []
    for entry in sorted(root.iterdir()):
        if _SINGLE_SLOT.fullmatch(entry.name) is None:
            continue
        try:
            raw = object_of(json.loads, entry.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"session record at {entry} is not valid JSON") from exc
        if not is_object_dict(raw):
            raise TypeError(f"session record at {entry} is not an object")
        record = record_from_payload(
            {"harness": _PRE_FIELD_HARNESS, "generation": 0, "background": [], **raw},
            source=str(entry),
        )
        if record.agent_id != int(entry.stem):
            raise ValueError(f"session record at {entry} names agent {record.agent_id}")
        if not registry.path(record.agent_id, record.harness).exists():
            registry.save(record)
        entry.unlink()
        migrated.append(entry)
    return migrated


# meiva:note
# What the registry would refuse in `root`, one line per file: a record still in the single-slot
# layout, or one the current code cannot read (a missing required field, malformed JSON). Empty
# on a converged home. The fleet update runs it on every host after the start, so a migration
# that did not run fails the window instead of surfacing as an agent that cannot start a session.
# meiva:link base/lm/harness/docs/harness.md
def unreadable_records(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    problems: list[str] = []
    for entry in sorted(root.iterdir()):
        if _SINGLE_SLOT.fullmatch(entry.name) is not None:
            problems.append(f"{entry}: single-slot layout")
        elif re.fullmatch(r"\d+\.[a-z][a-z0-9_]*\.json", entry.name) is not None:
            try:
                _ = FileSessionRegistry._read(entry)
            except (ValueError, TypeError) as exc:
                problems.append(str(exc))
    return problems


# meiva:note
# Complete the records written before `generation` or `background` existed: a missing `generation`
# becomes 0 (the first process that runs under it is generation 1) and a missing `background`
# becomes the empty list (the true value for a process that did not track background work), each
# written atomically in place. Both fields are required on read, so a home that was not converged
# refuses the record instead of resuming on a guess. Idempotent; returns the files changed. A file
# that is not a record fails naming it.
# meiva:link base/lm/harness/docs/harness.md
# meiva:link base/lm/harness/docs/background-work.md
def backfill_record_fields(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    changed: list[Path] = []
    for entry in sorted(root.iterdir()):
        if re.fullmatch(r"\d+\.[a-z][a-z0-9_]*\.json", entry.name) is None:
            continue
        try:
            raw = object_of(json.loads, entry.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"session record at {entry} is not valid JSON") from exc
        if not is_object_dict(raw):
            raise TypeError(f"session record at {entry} is not an object")
        if "generation" in raw and "background" in raw:
            continue
        completed: dict[str, object] = {"generation": 0, "background": [], **raw}
        record = record_from_payload(completed, source=str(entry))
        FileSessionRegistry(root).save(record)
        changed.append(entry)
    return changed


def _write_document(path: Path, document: dict[str, object]) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _check_agent(agent_id: int) -> None:
    if agent_id <= 0:
        raise ValueError(f"agent id must be positive, got {agent_id}")


# meiva:note
# The one place a stored or transferred record becomes a `SessionRecord`: the registry reads
# its own files through it and the cross-machine transfer rebuilds the record it received.
def record_from_payload(payload: dict[str, object], *, source: str) -> SessionRecord:
    try:
        return SessionRecord(
            agent_id=int_field(payload, "agent_id"),
            session_id=str_field(payload, "session_id"),
            cwd=str_field(payload, "cwd"),
            model=str_field(payload, "model"),
            created_at=str_field(payload, "created_at"),
            updated_at=str_field(payload, "updated_at"),
            harness=str_field(payload, "harness"),
            resumes=int_field(payload, "resumes") if "resumes" in payload else 0,
            generation=int_field(payload, "generation"),
            background=decode_items(payload["background"], source=source),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"session record at {source} has invalid values: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ActiveTurnRecord:
    turn_id: str
    started_at: str
    start_offset: int
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredTurnSpan:
    turn_id: str
    start_offset: int
    end_offset: int
    duration_ms: int


def _parse_closed_turn_row(raw_line: str, session_id: str) -> StoredTurnSpan | None:
    line = raw_line.strip()
    if not line:
        return None
    try:
        raw = object_of(json.loads, line)
    except ValueError:
        return None
    if not is_object_dict(raw) or raw.get("session_id") != session_id:
        return None
    tid = raw.get("turn_id")
    start_offset = raw.get("start_offset")
    end_offset = raw.get("end_offset")
    duration_ms = raw.get("duration_ms")
    if (
        isinstance(tid, str)
        and tid
        and isinstance(start_offset, int)
        and not isinstance(start_offset, bool)
        and isinstance(end_offset, int)
        and not isinstance(end_offset, bool)
        and isinstance(duration_ms, int)
        and not isinstance(duration_ms, bool)
    ):
        return StoredTurnSpan(
            turn_id=tid,
            start_offset=start_offset,
            end_offset=end_offset,
            duration_ms=max(duration_ms, 0),
        )
    return None


def _check_turn_identity(agent_id: int, harness: str) -> None:
    _check_agent(agent_id)
    if not _HARNESS_NAME.fullmatch(harness):
        raise ValueError(f"Invalid harness name: {harness!r}")


def _session_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


# meiva:note
# Per-`(agent_id, harness, session_id)` Turn-timing sidecar in `harness_sessions_dir`: writes an
# atomic `active_turn.json` marker at `TurnStarted` and appends one line to the session's
# `<digest>.turns.jsonl` at `TurnCompleted` in O(1), bounding cold-read parsing to that session.
class TurnTimingStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def active_path(self, agent_id: int, harness: str) -> Path:
        _check_turn_identity(agent_id, harness)
        return self._root / f"{agent_id}.{harness}.active_turn.json"

    def turns_path(self, agent_id: int, harness: str, *, session_id: str | None = None) -> Path:
        _check_turn_identity(agent_id, harness)
        if session_id:
            return self._root / f"{agent_id}.{harness}.{_session_digest(session_id)}.turns.jsonl"
        return self._root / f"{agent_id}.{harness}.turns.jsonl"

    def open_turn(
        self,
        agent_id: int,
        harness: str,
        *,
        turn_id: str,
        started_at: str,
        start_offset: int,
        session_id: str | None = None,
    ) -> None:
        path = self.active_path(agent_id, harness)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_document(
            path,
            {
                "turn_id": turn_id,
                "started_at": started_at,
                "start_offset": max(start_offset, 0),
                "session_id": session_id,
            },
        )

    def close_turn(
        self,
        agent_id: int,
        harness: str,
        *,
        session_id: str | None,
        turn_id: str,
        start_offset: int,
        end_offset: int,
        started_at: str,
        ended_at: str,
        duration_ms: int,
    ) -> None:
        self.clear_active(agent_id, harness)
        if not session_id or not turn_id:
            return
        safe_start = max(start_offset, 0)
        safe_end = max(end_offset, safe_start)
        row: dict[str, object] = {
            "session_id": session_id,
            "turn_id": turn_id,
            "start_offset": safe_start,
            "end_offset": safe_end,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": max(duration_ms, 0),
        }
        path = self.turns_path(agent_id, harness, session_id=session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    def clear_active(self, agent_id: int, harness: str) -> None:
        self.active_path(agent_id, harness).unlink(missing_ok=True)

    def load(
        self,
        agent_id: int,
        harness: str,
        *,
        session_id: str | None,
    ) -> tuple[tuple[StoredTurnSpan, ...], ActiveTurnRecord | None]:
        spans: list[StoredTurnSpan] = []
        if session_id:
            turns_file = self.turns_path(agent_id, harness, session_id=session_id)
            if turns_file.is_file():
                for raw_line in turns_file.read_text(encoding="utf-8").splitlines():
                    span = _parse_closed_turn_row(raw_line, session_id)
                    if span is not None:
                        spans.append(span)
        spans.sort(key=lambda s: s.start_offset)
        return tuple(spans), self._read_active(agent_id, harness, session_id=session_id)

    def _read_active(
        self,
        agent_id: int,
        harness: str,
        *,
        session_id: str | None,
    ) -> ActiveTurnRecord | None:
        path = self.active_path(agent_id, harness)
        if not path.is_file():
            return None
        try:
            raw = object_of(json.loads, path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not is_object_dict(raw):
            return None
        tid = raw.get("turn_id")
        started_at = raw.get("started_at")
        start_offset = raw.get("start_offset")
        rec_sid = raw.get("session_id")
        if (
            not isinstance(tid, str)
            or not tid
            or not isinstance(started_at, str)
            or not started_at
            or not isinstance(start_offset, int)
            or isinstance(start_offset, bool)
        ):
            return None
        if rec_sid is not None and session_id is not None and rec_sid != session_id:
            return None
        return ActiveTurnRecord(
            turn_id=tid,
            started_at=started_at,
            start_offset=max(start_offset, 0),
            session_id=rec_sid if isinstance(rec_sid, str) else None,
        )

    def delete_harnesses(self, agent_id: int, harnesses: Collection[str]) -> None:
        _check_agent(agent_id)
        for harness in harnesses:
            self.turns_path(agent_id, harness).unlink(missing_ok=True)
            self.active_path(agent_id, harness).unlink(missing_ok=True)
            if self._root.is_dir():
                pattern = re.compile(rf"{agent_id}\.{re.escape(harness)}\.[0-9a-f]+\.turns\.jsonl")
                for entry in self._root.iterdir():
                    if pattern.fullmatch(entry.name):
                        entry.unlink(missing_ok=True)

    def delete(self, agent_id: int) -> None:
        _check_agent(agent_id)
        if not self._root.is_dir():
            return
        pattern = re.compile(
            rf"{agent_id}\.[a-z][a-z0-9_]*(?:\.[0-9a-f]+)?\.(turns\.jsonl|active_turn\.json)"
        )
        for entry in self._root.iterdir():
            if pattern.fullmatch(entry.name):
                entry.unlink(missing_ok=True)


class SessionDigestStore(Protocol):
    def load(self, agent_id: int, harness: str, *, session_id: str) -> dict[str, str]: ...

    def save(
        self, agent_id: int, harness: str, *, session_id: str, digests: Mapping[str, str]
    ) -> None: ...


class InMemorySessionDigestStore:
    def __init__(self) -> None:
        self._records: dict[tuple[int, str, str], dict[str, str]] = {}

    def load(self, agent_id: int, harness: str, *, session_id: str) -> dict[str, str]:
        return dict(self._records.get((agent_id, harness, session_id), {}))

    def save(
        self, agent_id: int, harness: str, *, session_id: str, digests: Mapping[str, str]
    ) -> None:
        self._records[(agent_id, harness, session_id)] = dict(digests)


# meiva:note
# Per-`(agent_id, harness, session_id)` sidecar in `harness_sessions_dir` recording the content
# digests already delivered into that external session's context. A missing or corrupt sidecar
# reads as empty so upgraded legacy sessions and damaged records safely refresh on the next turn.
class FileSessionDigestStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def path(self, agent_id: int, harness: str, *, session_id: str) -> Path:
        _check_turn_identity(agent_id, harness)
        return (
            self._root / f"{agent_id}.{harness}.{_session_digest(session_id)}.context_digests.json"
        )

    def load(self, agent_id: int, harness: str, *, session_id: str) -> dict[str, str]:
        if not session_id:
            return {}
        target = self.path(agent_id, harness, session_id=session_id)
        try:
            raw = object_of(json.loads, target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not is_object_dict(raw) or raw.get("session_id") != session_id:
            return {}
        digests = raw.get("digests")
        if not is_object_dict(digests):
            return {}
        return {
            key: value
            for key, value in digests.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def save(
        self, agent_id: int, harness: str, *, session_id: str, digests: Mapping[str, str]
    ) -> None:
        if not session_id:
            return
        target = self.path(agent_id, harness, session_id=session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_document(target, {"session_id": session_id, "digests": dict(digests)})

    def delete_harnesses(self, agent_id: int, harnesses: Collection[str]) -> None:
        _check_agent(agent_id)
        if not self._root.is_dir():
            return
        for harness in harnesses:
            pattern = re.compile(
                rf"{agent_id}\.{re.escape(harness)}\.[0-9a-f]+\.context_digests\.json"
            )
            for entry in self._root.iterdir():
                if pattern.fullmatch(entry.name):
                    entry.unlink(missing_ok=True)

    def delete(self, agent_id: int) -> None:
        _check_agent(agent_id)
        if not self._root.is_dir():
            return
        pattern = re.compile(rf"{agent_id}\.[a-z][a-z0-9_]*\.[0-9a-f]+\.context_digests\.json")
        for entry in self._root.iterdir():
            if pattern.fullmatch(entry.name):
                entry.unlink(missing_ok=True)
