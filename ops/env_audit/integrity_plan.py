# meiva:note
# Whether the `.env` still matches its audit history, and what to do about it, as values.
#
# The guard is inert on a home that has neither the armed marker nor a history (a fresh home
# arms at its first audited change). Once armed, a missing, empty, unreadable or digest-less
# history is an anomaly that is reported and rebuilt, so deleting the history can never return
# a home to the unarmed state; a history whose last digest differs from the file's is an
# out-of-band modification. The shell reads the marker, the history and the file under the
# `.env` lock, executes the plan, and emits the event after the lock is released.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import assert_never, cast

__all__ = [
    "DigestlessHistory",
    "EmptyHistory",
    "History",
    "Intact",
    "NoHistory",
    "RecordedHistory",
    "Tampered",
    "UnreadableHistory",
    "is_armed",
    "plan_integrity",
    "recorded_history",
]


# meiva:note
# The history file does not exist.
@dataclass(frozen=True)
class NoHistory: ...


# meiva:note
# The history file exists but its last line cannot be read as a record.
@dataclass(frozen=True)
class UnreadableHistory: ...


# meiva:note
# The history file exists and holds no record.
@dataclass(frozen=True)
class EmptyHistory: ...


# meiva:note
# The last record names no digest, so it cannot vouch for the file.
@dataclass(frozen=True)
class DigestlessHistory: ...


# meiva:note
# The last record: the digest it vouches for, the key names the file held and the official
# write site that made it.
@dataclass(frozen=True)
class RecordedHistory:
    digest: str
    keys_after: frozenset[str]
    official_site: str


History = NoHistory | UnreadableHistory | EmptyHistory | DigestlessHistory | RecordedHistory


# meiva:note
# Read the last history record: its digest (`digest_after`, or the older `digest`), its key
# names (a malformed name array counts as no names) and the official site it names, carried
# through a chain of anomaly records.
def recorded_history(record: Mapping[str, object]) -> RecordedHistory | DigestlessHistory:
    digest = record.get("digest_after", record.get("digest"))
    if not isinstance(digest, str):
        return DigestlessHistory()
    site = record.get("site", record.get("last_official_site", ""))
    return RecordedHistory(
        digest=digest,
        keys_after=_names(record.get("keys_after")),
        official_site=site if isinstance(site, str) else "",
    )


def _names(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    names: set[str] = set()
    for name in cast("list[object]", value):
        if not isinstance(name, str):
            return frozenset()
        names.add(name)
    return frozenset(names)


# meiva:note
# The file matches its history. `mark_armed` asks the shell to write the missing marker, which
# upgrades a history that predates it without calling its valid last record an anomaly.
@dataclass(frozen=True)
class Intact:
    mark_armed: bool


# meiva:note
# The file or its history is not what an official write left.
#
# `reason` names a damaged history and is None for an out-of-band modification of a sound one.
# `rebuild_history` asks the shell to replace the history file first, because a damaged one is
# no trustworthy append target. `keys` are the names whose presence changed since the last
# record, and `digest` is the baseline the next check compares against.
@dataclass(frozen=True)
class Tampered:
    mark_armed: bool
    rebuild_history: bool
    reason: str | None
    digest: str
    last_official_site: str
    keys: tuple[str, ...]
    keys_after: tuple[str, ...]

    def record(self, ts: str) -> dict[str, object]:
        record: dict[str, object] = {
            "ts": ts,
            "kind": "unauthorized",
            "digest": self.digest,
            "last_official_site": self.last_official_site,
            "keys": list(self.keys),
            "keys_after": list(self.keys_after),
        }
        if self.reason is not None:
            record["reason"] = self.reason
        return record

    def event(self, machine: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "machine": machine,
            "last_official_site": self.last_official_site,
            "keys": list(self.keys),
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload

    def summary(self) -> str:
        if self.reason is not None:
            return f".env audit history tamper detected: {self.reason}"
        return (
            "out-of-band .env modification detected after official write at "
            f"{self.last_official_site}"
        )


IntegrityPlan = Intact | Tampered


def _reason(history: NoHistory | UnreadableHistory | EmptyHistory | DigestlessHistory) -> str:
    match history:
        case NoHistory():
            return "audit_history_missing"
        case UnreadableHistory():
            return "audit_history_corrupt"
        case EmptyHistory():
            return "audit_history_empty"
        case DigestlessHistory():
            return "audit_history_missing_digest"
        case _:
            assert_never(history)


# meiva:note
# The guard checks only a home that has the marker or a history; a fresh home has neither and
# stays unarmed until its first audited change.
def is_armed(*, marker: bool, history: History) -> bool:
    return marker or not isinstance(history, NoHistory)


# meiva:note
# Decide from what the shell read under the lock, for a home where `is_armed` holds.
def plan_integrity(
    *, marker: bool, history: History, current_digest: str, current_keys: frozenset[str]
) -> IntegrityPlan:
    if not is_armed(marker=marker, history=history):
        raise ValueError("the integrity guard does not check a home that is not armed")
    if isinstance(history, RecordedHistory):
        if history.digest == current_digest:
            return Intact(mark_armed=not marker)
        return Tampered(
            mark_armed=not marker,
            rebuild_history=False,
            reason=None,
            digest=current_digest,
            last_official_site=history.official_site,
            keys=tuple(sorted(current_keys ^ history.keys_after)),
            keys_after=tuple(sorted(current_keys)),
        )
    return Tampered(
        mark_armed=not marker,
        rebuild_history=True,
        reason=_reason(history),
        digest=current_digest,
        last_official_site="",
        keys=tuple(sorted(current_keys)),
        keys_after=tuple(sorted(current_keys)),
    )
