# meiva:note
# Post-turn local workspace hygiene scanner, cooldown state store, and system_note delivery
# (task #7521). Runs on the agent's own host after a normal turn settles to idle; never scans
# or wakes sleeping agents. Pure rules and message formatting live in `workspace_hygiene_policy.py`.

from __future__ import annotations

import asyncio
import json
import math
import os
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
from services.agent_runner.agent_host.lifecycle.workspace_hygiene_policy import (
    MAX_SCAN_DEPTH,
    MAX_SCAN_DIRS,
    NESTED_GIT_MARKER,
    WORKSPACE_HYGIENE_SOURCE,
    WORKSPACE_HYGIENE_STATE_FILENAME,
    DirectoryOverflow,
    HygieneCheckOutcome,
    HygieneScanResult,
    build_hygiene_inbound_payload,
    format_workspace_hygiene_reminder,
    is_excluded_entry,
    should_remind,
)
from services.agent_runner.agent_host.runtime import WorkspaceHygienePolicy

__all__ = [
    "check_and_remind_workspace_hygiene",
    "clear_last_reminded_at",
    "hygiene_state_path",
    "is_hygiene_turn_or_pending",
    "read_last_reminded_at",
    "scan_workspace_overflows",
    "write_last_reminded_at",
]

_STATE_TMP_SUFFIX = ".tmp"


def hygiene_state_path(workspace_root: Path) -> Path:
    return workspace_root / WORKSPACE_HYGIENE_STATE_FILENAME


def _is_entry_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


# meiva:note
# Count non-excluded direct entries and collect child dirs for one directory. A subdirectory
# containing `.git` (nested repo or worktree) is skipped completely: neither its entries nor
# its descendants are counted.
def _scan_single_directory(
    current_dir: Path,
    rel_path: str,
    *,
    depth: int,
    max_depth: int,
) -> tuple[int, list[tuple[str, str]]] | None:
    try:
        with os.scandir(current_dir) as it:
            entries = list(it)
    except OSError:
        return None

    if depth > 0 and any(entry.name == NESTED_GIT_MARKER for entry in entries):
        return None

    item_count = 0
    subdirs: list[tuple[str, str]] = []
    for entry in entries:
        is_dir = _is_entry_dir(entry)
        if is_excluded_entry(entry.name, depth=depth, is_dir=is_dir):
            continue
        item_count += 1
        if is_dir and depth < max_depth:
            child_rel = f"{rel_path}/{entry.name}" if rel_path else entry.name
            subdirs.append((entry.name, child_rel))

    subdirs.sort(key=lambda pair: pair[0])
    return item_count, subdirs


# meiva:note
# BFS-scan `workspace_root` with `os.scandir()` for directories over `max_items_per_dir`.
# Excludes dot-entries (`.*`), root system paths (`memory/`, `uploads/`,
# `meiva_instructions.md`), nested git repositories (subdirectories containing `.git`), and
# common build/dependency directories (`node_modules`, `__pycache__`, `dist`, `build`,
# `target`, `venv`). Never follows symlinks.
def scan_workspace_overflows(
    workspace_root: Path,
    *,
    max_items_per_dir: int,
    max_depth: int = MAX_SCAN_DEPTH,
    max_dirs: int = MAX_SCAN_DIRS,
) -> HygieneScanResult:
    try:
        if not workspace_root.is_dir():
            return HygieneScanResult(overflows=(), dirs_scanned=0, truncated=False)
    except OSError:
        return HygieneScanResult(overflows=(), dirs_scanned=0, truncated=False)

    overflows: list[DirectoryOverflow] = []
    queue: deque[tuple[Path, str, int]] = deque([(workspace_root, "", 0)])
    dirs_scanned = 0
    truncated = False

    while queue:
        if dirs_scanned >= max_dirs:
            truncated = True
            break
        current_dir, rel_path, depth = queue.popleft()
        dirs_scanned += 1
        scanned = _scan_single_directory(current_dir, rel_path, depth=depth, max_depth=max_depth)
        if scanned is None:
            continue
        item_count, subdirs = scanned
        if item_count > max_items_per_dir:
            overflows.append(
                DirectoryOverflow(relative_path=rel_path or ".", item_count=item_count)
            )
        for name, child_rel in subdirs:
            queue.append((current_dir / name, child_rel, depth + 1))

    overflows.sort(key=lambda item: (-item.item_count, item.relative_path))
    return HygieneScanResult(
        overflows=tuple(overflows),
        dirs_scanned=dirs_scanned,
        truncated=truncated,
    )


# meiva:note
# Read the persisted last-reminder unix timestamp, or None when absent/invalid.
def read_last_reminded_at(workspace_root: Path) -> float | None:
    path = hygiene_state_path(workspace_root)
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
    path = hygiene_state_path(workspace_root)
    tmp_path = path.with_name(f"{path.name}{_STATE_TMP_SUFFIX}")
    payload = json.dumps({"last_reminded_at": timestamp})
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)


# meiva:note
# Remove the persisted cooldown marker once the workspace is clean again.
def clear_last_reminded_at(workspace_root: Path) -> bool:
    path = hygiene_state_path(workspace_root)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class _HygieneGuardRow:
    latest_was_hygiene: bool
    has_pending: bool


# meiva:note
# Return True if the latest claimed batch was a hygiene reminder or one is already pending.
# Uses PostgreSQL's own `claimed_at` batch watermark so there is zero host-vs-DB clock skew
# and identical semantics across native `meiva` and external harnesses.
async def is_hygiene_turn_or_pending(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    agent_id: int,
) -> bool:
    tag = NoteTag.WORKSPACE_HYGIENE.value
    async with pool.connection() as conn:
        row = await aquery_one(
            conn,
            _HygieneGuardRow,
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
            "  ) AS latest_was_hygiene, "
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
    return row.latest_was_hygiene or row.has_pending


def _insert_hygiene_reminder(db: Database, bus: EventBus, agent_id: int, content: str) -> int:
    with db.write_transaction() as conn:
        return insert_inbound_message(
            conn,
            agent_id,
            content,
            WORKSPACE_HYGIENE_SOURCE,
            kind=InboundKind.SYSTEM_NOTE.value,
            payload=build_hygiene_inbound_payload(),
            database=db,
            bus=bus,
        )


# meiva:note
# Scan the local workspace after a normal turn and enqueue a reminder when due.
async def check_and_remind_workspace_hygiene(
    *,
    agent_id: int,
    workspace_root: Path,
    policy: WorkspaceHygienePolicy,
    now_epoch: float,
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    db: Database,
    bus: EventBus,
) -> HygieneCheckOutcome:
    if not policy.enabled:
        return HygieneCheckOutcome.DISABLED

    scan = scan_workspace_overflows(
        workspace_root,
        max_items_per_dir=policy.max_items_per_dir,
    )
    if not scan.overflows:
        cleared = clear_last_reminded_at(workspace_root)
        return HygieneCheckOutcome.CLEAN_RESET if cleared else HygieneCheckOutcome.CLEAN

    last_reminded = read_last_reminded_at(workspace_root)
    if not should_remind(
        last_reminded_at=last_reminded,
        now_epoch=now_epoch,
        cooldown_seconds=policy.remind_cooldown_seconds,
    ):
        return HygieneCheckOutcome.COOLDOWN_ACTIVE

    if await is_hygiene_turn_or_pending(pool, agent_id):
        return HygieneCheckOutcome.SKIPPED_SELF_TRIGGER

    content = format_workspace_hygiene_reminder(
        scan.overflows,
        max_items_per_dir=policy.max_items_per_dir,
    )
    await asyncio.to_thread(_insert_hygiene_reminder, db, bus, agent_id, content)
    write_last_reminded_at(workspace_root, now_epoch)
    logger.info(
        "workspace hygiene reminder enqueued for agent {agent_id} ({overflow_dirs} dir(s) over limit)",
        event="workspace_hygiene_reminded",
        agent_id=agent_id,
        overflow_dirs=len(scan.overflows),
        top_dir=scan.overflows[0].relative_path,
        top_count=scan.overflows[0].item_count,
    )
    return HygieneCheckOutcome.REMINDED
