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
from typing import cast

_STEM = "stand-down.json"


# meiva:note
# One home's declared stop; `until` is None when it waits for an explicit start.
@dataclass(frozen=True, slots=True)
class StandDown:
    since: datetime
    until: datetime | None
    reason: str | None
    machine: str | None

    def deadline_passed(self, now: datetime) -> bool:
        return self.until is not None and self.until <= now

    def describe(self, now: datetime | None = None) -> str:
        if self.until is None:
            return "stopped until an explicit start"
        left = int((self.until - (now or datetime.now(UTC))).total_seconds())
        if left <= 0:
            return f"declared window ended {-left}s ago"
        return f"declared window ends in {left}s"


def path(home: Path) -> Path:
    return home / "run" / "meiva-root" / _STEM


# meiva:note
# Persist the intent durably before any teardown starts: a host that dies
# mid-stop must still read as stopped on the next boot.
def record(
    home: Path, *, until: datetime | None, reason: str | None, machine: str | None = None
) -> StandDown:
    stand = StandDown(since=datetime.now(UTC), until=until, reason=reason, machine=machine)
    body = json.dumps(
        {
            "since": stand.since.isoformat(),
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
    if not isinstance(loaded, dict):
        return _unreadable()
    body = cast("dict[str, object]", loaded)
    reason = body.get("reason")
    machine = body.get("machine")
    return StandDown(
        since=_moment(body.get("since")) or datetime.now(UTC),
        until=_moment(body.get("until")),
        reason=reason if isinstance(reason, str) else None,
        machine=machine if isinstance(machine, str) else None,
    )


# meiva:note
# An unreadable record is a stop, not a licence to start: the helper treats its
# own unreadable marker the same way, and a wrong guess here starts a host an
# operator meant to leave down.
def _unreadable() -> StandDown:
    return StandDown(since=datetime.now(UTC), until=None, reason=None, machine=None)


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


# meiva:note
# The one question a boot job asks: is this home stopped on purpose right now?
def blocks_boot(home: Path, *, now: datetime | None = None) -> StandDown | None:
    stand = read(home)
    if stand is None:
        return None
    return None if stand.deadline_passed(now or datetime.now(UTC)) else stand


# meiva:note
# The line a start prints for this home's stand-down, or None when there is none:
# the OS boot job leaves a stopped home alone, and an explicit start is the one
# thing that ends a stand-down and says so.
def gate(home: Path, *, boot_attempt: bool, now: datetime | None = None) -> str | None:
    if boot_attempt:
        held = blocks_boot(home, now=now)
        if held is None:
            return None
        return f"stand-down ({held.describe(now)}): the boot job leaves this home stopped"
    if clear(home):
        return "stand-down cleared by an explicit start"
    return None


def window_until(seconds: int | None, *, now: datetime | None = None) -> datetime | None:
    if seconds is None:
        return None
    return (now or datetime.now(UTC)).replace(microsecond=0) + timedelta(seconds=seconds)
