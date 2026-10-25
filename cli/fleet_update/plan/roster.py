# meiva:note
# The roster gate of `up`: every listed machine online with its checkout and running code on one
# commit. A host this run already knows failed to start is expected to be offline, so waiting the
# whole roster window for it would turn one host's failure into everybody's timeout. Machines the
# roster lists that the window was not asked about are reported and never block.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import TypeGuard


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


@dataclass(frozen=True)
class RosterRow:
    name: str
    online: bool
    identity_mismatch: bool
    head_sha: str | None
    running_sha: str | None
    serve_agent_runner: bool


@dataclass(frozen=True)
class RosterSnapshot:
    listed: tuple[RosterRow, ...]
    offline: tuple[str, ...]
    commits: tuple[tuple[str | None, str | None], ...]
    commit: str | None

    @property
    def settled(self) -> bool:
        return (
            not self.offline
            and self.commit is not None
            and self.commits == ((self.commit, self.commit),)
        )


def _row(item: object) -> RosterRow:
    if not _is_object_dict(item):
        raise TypeError("a roster row must be a mapping")
    raw = item
    name, head, running = raw["name"], raw["head_sha"], raw["running_sha"]
    if not isinstance(name, str):
        raise TypeError("a roster row is named by text")
    return RosterRow(
        name=name,
        online=raw["online"] is True,
        identity_mismatch=raw["identity_mismatch"] is True,
        head_sha=head if isinstance(head, str) else None,
        running_sha=running if isinstance(running, str) else None,
        serve_agent_runner=raw["serve_agent_runner"] is True,
    )


# meiva:note
# The roster the shipped program printed: its last line, one JSON list. A reply that is not that
# is a failure of the gate with the reply's own words, not a crash.
def parse_roster(output: str) -> tuple[RosterRow, ...]:
    lines = output.splitlines()
    if not lines:
        raise ValueError("the roster program printed nothing")
    try:
        decoded: object = json.loads(lines[-1])
        if not _is_object_list(decoded):
            raise TypeError("roster payload must be a list")
        return tuple(_row(item) for item in decoded)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"the roster reply is not a roster ({type(exc).__name__}: {exc})") from exc


def missing_rows(rows: tuple[RosterRow, ...], wanted: Collection[str]) -> tuple[str, ...]:
    return tuple(sorted(set(wanted) - {row.name for row in rows}))


# meiva:note
# One roster read as the three facts the wait needs: the listed rows, which of them are offline
# (a host this run already failed is expected to be), and the commits the others report.
def snapshot(
    rows: tuple[RosterRow, ...], wanted: frozenset[str], expect_offline: frozenset[str]
) -> RosterSnapshot:
    listed = tuple(row for row in rows if row.name in wanted)
    committed = tuple(row for row in listed if row.name not in expect_offline)
    offline = tuple(row.name for row in committed if not row.online or row.identity_mismatch)
    commits = tuple(sorted({(row.head_sha, row.running_sha) for row in committed}, key=repr))
    return RosterSnapshot(
        listed=listed,
        offline=offline,
        commits=commits,
        commit=(committed or listed)[0].head_sha,
    )


def unlisted_lines(rows: tuple[RosterRow, ...], wanted: frozenset[str]) -> tuple[str, ...]:
    return tuple(
        f"roster: {row.name} is not listed, not checked: online={row.online} "
        f"head={row.head_sha} running={row.running_sha}"
        for row in rows
        if row.name not in wanted
    )


def timeout_message(timeout_s: int, seen: RosterSnapshot) -> str:
    commits = ", ".join(repr(pair) for pair in seen.commits)
    return f"roster after {timeout_s}s: offline {list(seen.offline)}; commits {{{commits}}}"


# meiva:note
# How long to sleep before reading the roster again, or None when the window is spent: the poll
# interval until the deadline, then give up with the last state.
def poll_delay(elapsed_s: float, timeout_s: int, poll_s: float) -> float | None:
    return None if elapsed_s >= timeout_s else poll_s
