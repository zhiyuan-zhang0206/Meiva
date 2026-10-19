# meiva:note
# Managed exec run directories have one supported removal path: this
# report-first pass. A request directory is disposable only when the agent's
# stored allocation set no longer holds it, no live process names its envelope,
# and it is older than the threshold; everything unprovable is retained.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from base.agents.incarnation.exec_request_evidence import live_request_references
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceEvidenceError,
    ResourceShapeError,
    decode_resources,
)
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import exec_domains_dir, exec_run_dir, meiva_home
from base.telemetry import process_name

# meiva:note
# One day is a retention margin, not a proof: settlement already removed the
# allocation, so age only keeps evidence available to whoever is still reading
# it. Every pass may override it, and nothing else depends on the value.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md
DEFAULT_MIN_AGE_HOURS = 24.0


# meiva:note
# One directory's disposition. Only `settled` is ever removed; `in-flight`,
# `recent` and `unknown` keep it and carry the reason into the report.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md
class Verdict(StrEnum):
    SETTLED = "settled"
    IN_FLIGHT = "in-flight"
    RECENT = "recent"
    UNKNOWN = "unknown"


# meiva:note
# `unsettled` is the exact request ids the agent's row still holds, or None when
# the row cannot be read: an unreadable set proves nothing, so every directory
# of that agent is retained instead of being assumed settled.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md
@dataclass(frozen=True)
class AgentResources:
    unsettled: frozenset[str] | None
    detail: str


@dataclass(frozen=True)
class DomainEntry:
    agent_id: int
    path: Path
    request: Path | None
    age_s: float
    size: int
    verdict: Verdict
    detail: str

    @property
    def disposable(self) -> bool:
        return self.verdict is Verdict.SETTLED

    def describe(self) -> str:
        return f"{self.path} [{self.verdict.value}]: {self.detail}"


@dataclass(frozen=True)
class RemovalReport:
    removed: tuple[DomainEntry, ...]
    failures: tuple[tuple[DomainEntry, str], ...]
    freed_bytes: int


# meiva:note
# The row is the only record that can prove a run settled: `register_exec`
# inserts the allocation before the child exists and `complete_exec` deletes it
# after the owner's close receipt, so an id absent from this set cannot still be
# running under a live incarnation.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md
def agent_resources(
    rows: Sequence[tuple[int, object, object, Any]],
) -> dict[int, AgentResources]:
    parsed: dict[int, AgentResources] = {}
    for agent_id, generation, owner, raw in rows:
        if raw is None:
            parsed[agent_id] = AgentResources(frozenset(), "the row stores no resource set")
            continue
        try:
            state = decode_resources(raw)
        except (ResourceEvidenceError, ResourceShapeError) as exc:
            parsed[agent_id] = AgentResources(None, f"stored resource set is unreadable: {exc}")
            continue
        if not isinstance(state, IncarnationResources):
            parsed[agent_id] = AgentResources(frozenset(), "the row stores a birth marker")
            continue
        if (state.generation, state.owner) != (generation, owner):
            parsed[agent_id] = AgentResources(
                None, "stored resource set belongs to another incarnation"
            )
            continue
        parsed[agent_id] = AgentResources(
            frozenset(state.requests), f"{len(state.requests)} unsettled request(s)"
        )
    return parsed


def read_resource_rows(
    db: Database, agent_ids: Sequence[int]
) -> tuple[tuple[int, object, object, Any], ...]:
    with db.write_transaction() as conn:
        rows = conn.execute(
            "SELECT id,runtime_generation,runtime_owner,incarnation_resources "
            "FROM agents_meta WHERE id = ANY(%s) ORDER BY id",
            (list(agent_ids),),
        ).fetchall()
    return tuple(rows)


def agents_with_domains(home: Path) -> tuple[int, ...]:
    try:
        entries = tuple(exec_run_dir(home).iterdir())
    except FileNotFoundError:
        return ()
    return tuple(
        sorted(
            int(entry.name)
            for entry in entries
            if entry.name.isdigit() and (entry / "domains").is_dir()
        )
    )


def domain_directories(agent_id: int, *, home: Path) -> tuple[Path, ...]:
    try:
        return tuple(sorted(exec_domains_dir(home, agent_id).iterdir()))
    except FileNotFoundError:
        return ()


def _request_path(path: Path) -> tuple[Path | None, str | None]:
    try:
        request = UUID(path.name)
    except ValueError:
        return None, "directory name is not a request id"
    expected = path / f"req-{request.hex}.json"
    found = sorted(entry.name for entry in path.glob("req-*.json"))
    if found != [expected.name]:
        return None, f"expected only {expected.name}, found {found or 'none'}"
    return expected, None


def _tree_size(path: Path) -> int:
    total = 0
    for root, _directories, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


# meiva:note
# The four legs are checked cheapest first and every failure retains the
# directory with its reason, so a report always explains what it left behind.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md
def classify_domain(
    path: Path,
    *,
    agent_id: int,
    stored: AgentResources,
    live: frozenset[str],
    min_age_s: float,
    now: float,
) -> DomainEntry:
    if path.is_symlink():
        return DomainEntry(agent_id, path, None, 0.0, 0, Verdict.UNKNOWN, "directory is a symlink")
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        return DomainEntry(agent_id, path, None, 0.0, 0, Verdict.UNKNOWN, f"cannot stat: {exc}")
    age, size = max(0.0, now - mtime), _tree_size(path)
    request, problem = _request_path(path)
    if request is None or problem is not None:
        return DomainEntry(agent_id, path, None, age, size, Verdict.UNKNOWN, problem or "")
    if stored.unsettled is None:
        return DomainEntry(agent_id, path, request, age, size, Verdict.UNKNOWN, stored.detail)
    if str(UUID(path.name)) in stored.unsettled:
        return DomainEntry(
            agent_id,
            path,
            request,
            age,
            size,
            Verdict.IN_FLIGHT,
            "the stored allocation set still holds this request",
        )
    if os.path.realpath(request) in live:
        return DomainEntry(
            agent_id,
            path,
            request,
            age,
            size,
            Verdict.IN_FLIGHT,
            "a live process names this request envelope",
        )
    if age < min_age_s:
        return DomainEntry(
            agent_id,
            path,
            request,
            age,
            size,
            Verdict.RECENT,
            f"{age / 3600:.1f}h old, under the {min_age_s / 3600:.1f}h threshold",
        )
    return DomainEntry(
        agent_id,
        path,
        request,
        age,
        size,
        Verdict.SETTLED,
        "settled, unreferenced, and past the age threshold",
    )


def survey_domains(
    agent_ids: Sequence[int],
    *,
    home: Path,
    stored: Mapping[int, AgentResources],
    live: frozenset[str],
    min_age_s: float,
    now: float | None = None,
) -> tuple[DomainEntry, ...]:
    moment = time.time() if now is None else now
    entries: list[DomainEntry] = []
    for agent_id in agent_ids:
        state = stored.get(agent_id, AgentResources(frozenset(), "no agents_meta row"))
        for path in domain_directories(agent_id, home=home):
            entries.append(
                classify_domain(
                    path,
                    agent_id=agent_id,
                    stored=state,
                    live=live,
                    min_age_s=min_age_s,
                    now=moment,
                )
            )
    return tuple(entries)


def remove_settled(
    entries: Sequence[DomainEntry], *, remove: Callable[[Path], None] = shutil.rmtree
) -> RemovalReport:
    removed: list[DomainEntry] = []
    failures: list[tuple[DomainEntry, str]] = []
    freed = 0
    for entry in entries:
        if not entry.disposable:
            continue
        try:
            remove(entry.path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append((entry, str(exc)))
        else:
            removed.append(entry)
            freed += entry.size
    return RemovalReport(tuple(removed), tuple(failures), freed)


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _summarize(entries: Sequence[DomainEntry]) -> str:
    settled = [entry for entry in entries if entry.disposable]
    counts = {
        verdict: sum(1 for entry in entries if entry.verdict is verdict) for verdict in Verdict
    }
    return (
        f"{len(entries)} directories: {len(settled)} settled "
        f"({_human_bytes(sum(entry.size for entry in settled))}), "
        f"{counts[Verdict.IN_FLIGHT]} in-flight, {counts[Verdict.RECENT]} recent, "
        f"{counts[Verdict.UNKNOWN]} unknown"
    )


# meiva:note
# `print` is lint-banned in `base/`; every report line goes through one writer.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md
def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")


# meiva:note
# The pass takes the read rows and the live-reference set as parameters: the
# CLI is the only place that builds them, so the rule above can be exercised
# without a cluster.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md
def cleanup_pass(
    agent_ids: Sequence[int],
    *,
    home: Path,
    rows: Sequence[tuple[int, object, object, Any]],
    live: frozenset[str],
    delete: bool,
    min_age_s: float,
    emit: Callable[[str], None] = _emit,
) -> int:
    if not agent_ids:
        emit("no agent run/exec directory under this home")
        return 0
    stored = agent_resources(rows)
    entries = survey_domains(agent_ids, home=home, stored=stored, live=live, min_age_s=min_age_s)
    by_agent: dict[int, list[DomainEntry]] = {}
    for entry in entries:
        by_agent.setdefault(entry.agent_id, []).append(entry)
    for agent_id, agent_entries in by_agent.items():
        emit(f"agent {agent_id}: {_summarize(agent_entries)}")
        for entry in agent_entries:
            if entry.verdict in {Verdict.IN_FLIGHT, Verdict.UNKNOWN}:
                emit(f"  {entry.describe()}")
    settled = [entry for entry in entries if entry.disposable]
    if not delete:
        emit(
            f"report only: {len(settled)} settled directories, "
            f"{_human_bytes(sum(entry.size for entry in settled))}; "
            "pass --delete to remove them"
        )
        return 0
    report = remove_settled(entries)
    emit(f"removed {len(report.removed)} directories, freed {_human_bytes(report.freed_bytes)}")
    for entry, error in report.failures:
        emit(f"  failed {entry.path}: {error}")
    if report.removed:
        logger.info(
            "settled exec domain directories removed: {removed} directories, {freed} bytes",
            event="exec_domain_cleanup",
            agents=sorted(by_agent),
            removed=len(report.removed),
            freed=report.freed_bytes,
        )
    return 1 if report.failures else 0


# meiva:note
# Report first and delete only on `--delete`, because the leftover directories
# are the only local evidence of a run whose parent died before it settled.
# Exit status is 1 only when a requested removal failed.
# meiva:link base/agents/incarnation/docs/exec-domain-cleanup.meiva.okf.md
def main(argv: Sequence[str] | None = None, *, home: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m base.agents.incarnation.exec_domain_cleanup",
        description=(
            "Report managed exec run directories under run/exec/<agent>/domains, or remove "
            "the settled ones. A directory is settled when the agent's stored allocation set "
            "no longer holds its request, no live process names its envelope, and it is past "
            "the age threshold. Never glob the run tree to do this by hand."
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--agent", type=int, help="one agent id to inspect")
    target.add_argument(
        "--all-agents", action="store_true", help="every agent directory under this home"
    )
    parser.add_argument(
        "--older-than-hours",
        type=float,
        default=DEFAULT_MIN_AGE_HOURS,
        metavar="HOURS",
        help=f"retention margin before a settled directory becomes removable "
        f"(default {DEFAULT_MIN_AGE_HOURS:g})",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="remove the settled directories instead of only reporting them",
    )
    args = parser.parse_args(argv)
    if args.older_than_hours < 0:
        parser.error("--older-than-hours must not be negative")
    agent_ids = (args.agent,) if args.agent is not None else agents_with_domains(home)
    if not agent_ids:
        _emit("no agent run/exec directory under this home")
        return 0
    version = CodeVersion(LoadedCommit.capture())
    db = Database.from_settings(gate=ProcessDbGate(version=version.get, process=process_name()))
    return cleanup_pass(
        agent_ids,
        home=home,
        rows=read_resource_rows(db, agent_ids),
        live=live_request_references(),
        delete=args.delete,
        min_age_s=args.older_than_hours * 3600.0,
    )


if __name__ == "__main__":
    raise SystemExit(main(home=meiva_home()))
