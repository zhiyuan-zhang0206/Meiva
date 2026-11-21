# meiva:note
# Post-turn local personal memory scanner, cooldown state store, and system_note delivery for
# Personal Self-Dream (task #7522). Runs on the agent's own host after a normal turn settles to
# idle; never scans or wakes sleeping agents. Pure rules and message formatting live in
# `personal_dream_policy.py`.

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.inbound import InboundKind
from base.agents.messages.kwargs import NoteTag
from base.db import Database
from base.db.rows import aquery_one
from base.events.live.bus import EventBus
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.log import logger
from services.agent_runner.agent_host.lifecycle.personal_dream_policy import (
    MAX_SCAN_DEPTH,
    MAX_SCAN_DIRS,
    PERSONAL_DREAM_SOURCE,
    PERSONAL_DREAM_STATE_FILENAME,
    PERSONAL_MEMORY_DIRNAME,
    PERSONAL_MEMORY_INDEX_FILENAME,
    PersonalDreamOutcome,
    PersonalMemoryScan,
    build_personal_dream_inbound_payload,
    evaluate_personal_dream,
    format_personal_dream_reminder,
    is_cooldown_elapsed,
    is_excluded_memory_entry,
)
from services.agent_runner.agent_host.runtime import PersonalDreamPolicy

__all__ = [
    "check_and_remind_personal_dream",
    "is_personal_dream_turn_or_pending",
    "personal_dream_state_path",
    "personal_memory_path",
    "read_last_reminded_at",
    "scan_personal_memory",
    "write_last_reminded_at",
]

_STATE_TMP_SUFFIX = ".tmp"


def personal_dream_state_path(workspace_root: Path) -> Path:
    return workspace_root / PERSONAL_DREAM_STATE_FILENAME


def personal_memory_path(workspace_root: Path) -> Path:
    return workspace_root / PERSONAL_MEMORY_DIRNAME


def _entry_is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _entry_is_file(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _entry_mtime(entry: os.DirEntry[str]) -> float | None:
    try:
        mtime = float(entry.stat(follow_symlinks=False).st_mtime)
    except OSError:
        return None
    return mtime if math.isfinite(mtime) else None


def _read_index_lines(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return 0
    return len(text.splitlines()) if text else 0


def _update_mtime_bounds(
    earliest: float | None,
    latest: float | None,
    mtime: float | None,
) -> tuple[float | None, float | None]:
    if mtime is None:
        return earliest, latest
    next_earliest = mtime if earliest is None else min(earliest, mtime)
    next_latest = mtime if latest is None else max(latest, mtime)
    return next_earliest, next_latest


@dataclass(frozen=True)
class _DirScanDelta:
    index_lines: int
    file_count: int
    earliest_mtime: float | None
    latest_mtime: float | None
    subdirs: tuple[str, ...]


def _is_real_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def _scan_memory_directory(
    current_dir: Path,
    *,
    depth: int,
    max_depth: int,
) -> _DirScanDelta | None:
    try:
        with os.scandir(current_dir) as it:
            entries = list(it)
    except OSError:
        return None

    index_lines = 0
    file_count = 0
    earliest_mtime: float | None = None
    latest_mtime: float | None = None
    subdirs: list[str] = []

    for entry in entries:
        if is_excluded_memory_entry(entry.name):
            continue
        if _entry_is_dir(entry):
            if depth < max_depth:
                subdirs.append(entry.name)
            continue
        if not _entry_is_file(entry):
            continue

        mtime = _entry_mtime(entry)
        if depth == 0 and entry.name == PERSONAL_MEMORY_INDEX_FILENAME:
            index_lines = _read_index_lines(current_dir / entry.name)
            if index_lines > 0:
                earliest_mtime, latest_mtime = _update_mtime_bounds(
                    earliest_mtime, latest_mtime, mtime
                )
        else:
            file_count += 1
            earliest_mtime, latest_mtime = _update_mtime_bounds(earliest_mtime, latest_mtime, mtime)

    subdirs.sort()
    return _DirScanDelta(
        index_lines=index_lines,
        file_count=file_count,
        earliest_mtime=earliest_mtime,
        latest_mtime=latest_mtime,
        subdirs=tuple(subdirs),
    )


# meiva:note
# Bounded BFS scan of `<workspace>/memory/` using `os.scandir()` without following symlinks.
# Excludes dot-entries (`.*`). Measures `MEMORY.md` line count separately from note `file_count`,
# and only includes `MEMORY.md`'s mtime when it has non-whitespace content so an auto-scaffolded
# empty index does not fabricate memory age.
def scan_personal_memory(
    workspace_root: Path,
    *,
    max_depth: int = MAX_SCAN_DEPTH,
    max_dirs: int = MAX_SCAN_DIRS,
) -> PersonalMemoryScan:
    mem_dir = personal_memory_path(workspace_root)
    if not _is_real_dir(mem_dir):
        return PersonalMemoryScan(
            index_lines=0,
            file_count=0,
            earliest_mtime=None,
            latest_mtime=None,
            dirs_scanned=0,
            truncated=False,
        )

    index_lines = 0
    file_count = 0
    earliest_mtime: float | None = None
    latest_mtime: float | None = None
    queue: deque[tuple[Path, int]] = deque([(mem_dir, 0)])
    dirs_scanned = 0
    truncated = False

    while queue:
        if dirs_scanned >= max_dirs:
            truncated = True
            break
        current_dir, depth = queue.popleft()
        dirs_scanned += 1
        delta = _scan_memory_directory(current_dir, depth=depth, max_depth=max_depth)
        if delta is None:
            continue
        index_lines += delta.index_lines
        file_count += delta.file_count
        earliest_mtime, latest_mtime = _update_mtime_bounds(
            earliest_mtime, latest_mtime, delta.earliest_mtime
        )
        earliest_mtime, latest_mtime = _update_mtime_bounds(
            earliest_mtime, latest_mtime, delta.latest_mtime
        )
        for name in delta.subdirs:
            queue.append((current_dir / name, depth + 1))

    return PersonalMemoryScan(
        index_lines=index_lines,
        file_count=file_count,
        earliest_mtime=earliest_mtime,
        latest_mtime=latest_mtime,
        dirs_scanned=dirs_scanned,
        truncated=truncated,
    )


# meiva:note
# Read the persisted last-reminder unix timestamp, or None when absent/invalid.
def read_last_reminded_at(workspace_root: Path) -> float | None:
    path = personal_dream_state_path(workspace_root)
    try:
        raw = object_of(json.loads, path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not is_object_dict(raw):
        return None
    value = raw.get("last_reminded_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timestamp = float(value)
    return timestamp if math.isfinite(timestamp) else None


# meiva:note
# Persist the last-reminder unix timestamp atomically inside the workspace.
def write_last_reminded_at(workspace_root: Path, timestamp: float) -> None:
    path = personal_dream_state_path(workspace_root)
    tmp_path = path.with_name(f"{path.name}{_STATE_TMP_SUFFIX}")
    payload = json.dumps({"last_reminded_at": timestamp})
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)


@dataclass(frozen=True)
class _PersonalDreamGuardRow:
    latest_was_personal_dream: bool
    has_pending: bool


# meiva:note
# Return True if the latest claimed batch was a Personal Self-Dream reminder or one is already
# pending. Uses PostgreSQL's own `claimed_at` batch watermark so there is zero host-vs-DB clock
# skew and identical semantics across native `meiva` and external harnesses.
async def is_personal_dream_turn_or_pending(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    agent_id: int,
) -> bool:
    tag = NoteTag.PERSONAL_DREAM.value
    async with pool.connection() as conn:
        row = await aquery_one(
            conn,
            _PersonalDreamGuardRow,
            "WITH latest_claim AS ("
            "  SELECT claimed_at "
            "  FROM inbound_messages "
            "  WHERE agent_id = %s "
            "    AND status IN ('claimed', 'done') "
            "    AND claimed_at IS NOT NULL "
            "    AND kind IN ('chat', 'system_note', 'heartbeat') "
            "  ORDER BY claimed_at DESC, id DESC "
            "  LIMIT 1"
            ") "
            "SELECT "
            "  EXISTS ("
            "    SELECT 1 FROM inbound_messages m, latest_claim lc "
            "    WHERE m.agent_id = %s "
            "      AND m.status IN ('claimed', 'done') "
            "      AND m.claimed_at = lc.claimed_at "
            "      AND m.kind = 'system_note' "
            "      AND m.payload ->> 'note_tag' = %s"
            "  ) AS latest_was_personal_dream, "
            "  EXISTS ("
            "    SELECT 1 FROM inbound_messages p "
            "    WHERE p.agent_id = %s "
            "      AND p.status = 'pending' "
            "      AND p.kind = 'system_note' "
            "      AND p.payload ->> 'note_tag' = %s"
            "  ) AS has_pending",
            (agent_id, agent_id, tag, agent_id, tag),
        )
    if row is None:
        return False
    return row.latest_was_personal_dream or row.has_pending


def _insert_personal_dream_reminder(
    db: Database, bus: EventBus, agent_id: int, content: str
) -> int:
    with db.write_transaction() as conn:
        return insert_inbound_message(
            conn,
            agent_id,
            content,
            PERSONAL_DREAM_SOURCE,
            kind=InboundKind.SYSTEM_NOTE.value,
            payload=build_personal_dream_inbound_payload(),
            database=db,
            bus=bus,
        )


# meiva:note
# Scan the local `<workspace>/memory/` after a normal turn and enqueue a Personal Self-Dream
# reminder when capacity or active-change criteria match past cooldown.
async def check_and_remind_personal_dream(
    *,
    agent_id: int,
    workspace_root: Path,
    policy: PersonalDreamPolicy,
    now_epoch: float,
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    db: Database,
    bus: EventBus,
    shared_memory_path: Path | None = None,
) -> PersonalDreamOutcome:
    if not policy.enabled:
        return PersonalDreamOutcome.DISABLED

    scan = scan_personal_memory(workspace_root)
    if scan.is_empty:
        return PersonalDreamOutcome.EMPTY

    last_reminded = read_last_reminded_at(workspace_root)
    if not is_cooldown_elapsed(
        last_reminded_at=last_reminded,
        now_epoch=now_epoch,
        cooldown_seconds=policy.cooldown_seconds,
    ):
        return PersonalDreamOutcome.COOLDOWN_ACTIVE

    decision = evaluate_personal_dream(
        scan,
        last_reminded_at=last_reminded,
        now_epoch=now_epoch,
        cooldown_seconds=policy.cooldown_seconds,
        index_max_lines=policy.index_max_lines,
        max_files=policy.max_files,
    )
    if not decision.should_trigger or decision.reason is None:
        return PersonalDreamOutcome.NOT_DUE

    if await is_personal_dream_turn_or_pending(pool, agent_id):
        return PersonalDreamOutcome.SKIPPED_SELF_TRIGGER

    shared_display = (
        str(shared_memory_path) if shared_memory_path is not None else "~/.meiva/vault/memory"
    )
    content = format_personal_dream_reminder(
        scan,
        decision,
        index_max_lines=policy.index_max_lines,
        max_files=policy.max_files,
        shared_memory_path=shared_display,
    )
    await asyncio.to_thread(_insert_personal_dream_reminder, db, bus, agent_id, content)
    write_last_reminded_at(workspace_root, now_epoch)
    logger.info(
        "personal Self-Dream reminder enqueued for agent {agent_id} "
        "(reason={reason}, index_lines={index_lines}, file_count={file_count})",
        event="personal_dream_reminded",
        agent_id=agent_id,
        reason=decision.reason.value,
        index_lines=scan.index_lines,
        file_count=scan.file_count,
    )
    return PersonalDreamOutcome.REMINDED
