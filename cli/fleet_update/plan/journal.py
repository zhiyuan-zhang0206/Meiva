# meiva:note
# What a host's lifecycle journal (`run/lifecycle-op.json`) says about how its last operation
# ended. The journal is the host's own evidence: a stop that exited 0 still counts as failed
# unless the journal records it complete with rc 0.
# meiva:link cli/docs/fleet-update-window/failed-stop-recovery.meiva.okf.md

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeGuard


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


@dataclass(frozen=True)
class Journal:
    operation: str | None
    complete: bool
    rc: int | None


@dataclass(frozen=True)
class HostEvidence:
    daemon_up: bool
    journal: Journal | None


# meiva:note
# The journal a probe line reports. An unreadable record is no journal, never a crash inside the
# gate that is deciding whether to refuse.
def parse_journal(text: str) -> Journal | None:
    try:
        parsed: object = json.loads(text)
    except ValueError:
        return None
    if not _is_object_dict(parsed):
        return None
    record = parsed
    operation = record.get("operation")
    result = record.get("result")
    rc = result.get("rc") if _is_object_dict(result) else None
    return Journal(
        operation=operation if isinstance(operation, str) else None,
        complete=record.get("complete") is True,
        rc=rc if isinstance(rc, int) and not isinstance(rc, bool) else None,
    )


# meiva:note
# A stop is complete when its journal says so with rc 0. Another operation, or no journal, says
# nothing about a stop.
def stop_completed(journal: Journal | None) -> bool:
    return (
        journal is not None and journal.operation == "stop" and journal.complete and journal.rc == 0
    )


# meiva:note
# A stop record that is not a completed stop: the stop failed or never finished.
def stop_failed(journal: Journal | None) -> bool:
    return journal is not None and journal.operation == "stop" and not stop_completed(journal)


# meiva:note
# The host's daemon state and its last journal, read in one go from the evidence script's lines.
def parse_evidence(output: str) -> HostEvidence:
    lines = output.splitlines()
    raw = next((line for line in lines if line.startswith("{")), None)
    return HostEvidence(
        daemon_up=any(line.strip() == "host=up" for line in lines),
        journal=parse_journal(raw) if raw is not None else None,
    )
