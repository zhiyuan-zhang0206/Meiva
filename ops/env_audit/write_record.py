# meiva:note
# The two shapes an official `.env` write leaves behind, as values: the owner-only JSONL
# record and the value-free `env_write` event.
#
# The record keeps key names, the post-write key set and digest, the initiating credential
# fact and, for registered non-sensitive aliases, the old to new diff. The event repeats the
# names and never a value. The shell reads the clock, the pid and the file; this module only
# arranges what it was given.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ops.env_audit.redaction import RedactedChange

__all__ = ["WriteFacts", "write_event", "write_record"]


# meiva:note
# What one official write looked like when it landed.
@dataclass(frozen=True)
class WriteFacts:
    ts: str
    site: str
    pid: int
    process: str
    cmdline: str
    actor: str | None
    trace_id: str | None
    keys_written: frozenset[str]
    keys_removed: frozenset[str]
    keys_after: tuple[str, ...]
    digest_after: str


# meiva:note
# The JSONL record (v2) of a write. `changed` is absent when the caller supplied no diff.
def write_record(facts: WriteFacts, changed: Sequence[RedactedChange] | None) -> dict[str, object]:
    record: dict[str, object] = {
        "ts": facts.ts,
        "site": facts.site,
        "pid": facts.pid,
        "process": facts.process,
        "cmdline": facts.cmdline,
        "actor": facts.actor,
        "trace_id": facts.trace_id,
        "keys_written": sorted(facts.keys_written),
        "keys_removed": sorted(facts.keys_removed),
        "keys_after": list(facts.keys_after),
        "digest_after": facts.digest_after,
    }
    if changed is not None:
        record["changed"] = [change.as_record() for change in changed]
    return record


# meiva:note
# The `env_write` event payload: names and the actor, never a value.
def write_event(facts: WriteFacts) -> dict[str, object]:
    return {
        "site": facts.site,
        "actor": facts.actor,
        "pid": facts.pid,
        "process": facts.process,
        "cmdline": facts.cmdline,
        "keys_written": sorted(facts.keys_written),
        "keys_removed": sorted(facts.keys_removed),
        "digest_after": facts.digest_after,
    }
