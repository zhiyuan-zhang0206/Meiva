from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from base.host.boundary.fields import int_field, str_field
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

_REQUIRED_FIELDS = frozenset(
    {"agent_id", "session_id", "cwd", "model", "created_at", "updated_at", "harness"}
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
    resumes: int = 0


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
# agent to another machine, never for a switch.
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
        payload = json.dumps(asdict(record), indent=2, sort_keys=True) + "\n"
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def delete(self, agent_id: int) -> bool:
        _check_agent(agent_id)
        doomed = [self._legacy_path(agent_id)]
        if self._root.is_dir():
            pattern = re.compile(rf"{agent_id}\.[a-z][a-z0-9_]*\.json")
            doomed += [entry for entry in self._root.iterdir() if pattern.fullmatch(entry.name)]
        removed = False
        for entry in doomed:
            try:
                entry.unlink()
            except FileNotFoundError:
                continue
            removed = True
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
        record = record_from_payload({"harness": _PRE_FIELD_HARNESS, **raw}, source=str(entry))
        if record.agent_id != int(entry.stem):
            raise ValueError(f"session record at {entry} names agent {record.agent_id}")
        if not registry.path(record.agent_id, record.harness).exists():
            registry.save(record)
        entry.unlink()
        migrated.append(entry)
    return migrated


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
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"session record at {source} has invalid values: {exc}") from exc
