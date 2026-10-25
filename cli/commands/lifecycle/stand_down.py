# meiva:note
# A stop that says "stopped" outranks the keepers that exist to survive a crash.
#
# `meiva stop` leaves the host down on purpose. On macOS the permissions helper
# keeps the root alive and restarts a root that died; it reads its own marker
# (`run/meiva-root/root-stopped`) and this record explains that marker to the OS
# boot job (`com.meiva.autostart` -> `meiva start --boot`), which otherwise
# re-seeds the root at login and clears the marker on the way. The boot job reads
# this record and leaves the host alone, so an operator's stop is not undone by
# machinery meant for an accidental death (task #5493). Only an explicit
# `meiva start` ends a stand-down without a deadline; a stop that declared
# `--maintenance-for T` lets the boot job resume after that instant, and the
# existing `machine_stopped_too_long` alert (20 minutes) catches a stop nobody
# came back for.
# meiva:link cli/commands/lifecycle/docs/stand-down.meiva.okf.md

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeGuard

_STEM = "stand-down.json"


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# One home's declared stop; `until` is None when it waits for an explicit start.
@dataclass(frozen=True, slots=True)
class StandDown:
    # meiva:note
    # None when the record does not say (an unreadable or older record).
    since: datetime | None
    until: datetime | None
    reason: str | None
    machine: str | None

    def deadline_passed(self, now: datetime) -> bool:
        return self.until is not None and self.until <= now

    def describe(self, now: datetime) -> str:
        if self.until is None:
            return "stopped until an explicit start"
        left = int((self.until - now).total_seconds())
        if left <= 0:
            return f"declared window ended {-left}s ago"
        return f"declared window ends in {left}s"


def path(home: Path) -> Path:
    return home / "run" / "meiva-root" / _STEM


# meiva:note
# Persist the intent durably before any teardown starts: a host that dies
# mid-stop must still read as stopped on the next boot.
def record(
    home: Path,
    *,
    now: datetime,
    until: datetime | None,
    reason: str | None,
    machine: str | None = None,
) -> StandDown:
    stand = StandDown(since=now, until=until, reason=reason, machine=machine)
    body = json.dumps(
        {
            "since": now.isoformat(),
            "until": stand.until.isoformat() if stand.until else None,
            "reason": stand.reason,
            "machine": stand.machine,
        },
        sort_keys=True,
    )
    from base.host.atomic_io import write_text_atomic

    target = path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, body, mode=0o600, sync_parent=True)
    return stand


# meiva:note
# A record that cannot be read or parsed is not a licence to start: an unreadable
# stop intent keeps the host down, the same way an unreadable helper intent does.
def read(home: Path) -> StandDown | None:
    target = path(home)
    try:
        text = target.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return _unreadable()
    try:
        loaded: object = json.loads(text)
    except ValueError:
        return _unreadable()
    if not _is_object_dict(loaded):
        return _unreadable()
    reason = loaded.get("reason")
    machine = loaded.get("machine")
    return StandDown(
        since=_moment(loaded.get("since")),
        until=_moment(loaded.get("until")),
        reason=reason if isinstance(reason, str) else None,
        machine=machine if isinstance(machine, str) else None,
    )


# meiva:note
# An unreadable record is a stop, not a licence to start: the helper treats its
# own unreadable marker the same way, and a wrong guess here starts a host an
# operator meant to leave down.
def _unreadable() -> StandDown:
    return StandDown(since=None, until=None, reason=None, machine=None)


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def clear(home: Path) -> bool:
    try:
        path(home).unlink()
    except FileNotFoundError:
        return False
    return True


def window_until(seconds: int | None, *, now: datetime) -> datetime | None:
    if seconds is None:
        return None
    return now.replace(microsecond=0) + timedelta(seconds=seconds)
