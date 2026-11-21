# meiva:note
# Antigravity (`agy`) session collection and relocation for cross-machine agent transfers.
#
# Collects `brain/<session_id>` and `conversations/<session_id>.*` while snapshotting SQLite
# conversation databases into a single self-contained image (folding any uncheckpointed WAL
# frames and omitting non-portable `.db-shm` files). On target restore, rewrites persisted
# `file://` workspace URIs in SQLite `trajectory_metadata_blob`, legacy `.pb` trajectories,
# brain `.jsonl` logs, and any pre-existing target `conversation_summaries.db` row.
#
# meiva:link gateway/agents/docs/agent-transfer.md

from __future__ import annotations

import base64
import contextlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import TypeGuard

from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_of
from ops.agent_transfer.agy_protobuf import (
    path_to_file_uri,
    rewrite_cascade_summary_pb,
    rewrite_trajectory_metadata_pb,
    rewrite_trajectory_pb,
    rewrite_workspace_uris_json,
)
from ops.rpc_schemas.transfer import HarnessTransferFile, HarnessTransferPayload

_log = logging.getLogger(__name__)
_SQLITE_HEADER = b"SQLite format 3\x00"


def _is_row_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _snapshot_sqlite_bytes(db_path: Path) -> bytes:
    with (
        contextlib.closing(sqlite3.connect(db_path)) as src_conn,
        contextlib.closing(sqlite3.connect(":memory:")) as mem_conn,
    ):
        src_conn.backup(mem_conn)
        return mem_conn.serialize()


def _collect_conversation_files(
    u_home: Path,
    conv_dir: Path,
    session_id: str,
    files: list[HarnessTransferFile],
) -> None:
    candidates = [cp for cp in sorted(conv_dir.glob(f"{session_id}.*")) if cp.is_file()]
    snapshotted_dbs: set[str] = set()
    for cp in candidates:
        if cp.suffix != ".db" or not _is_sqlite_file(cp):
            continue
        try:
            rel = cp.relative_to(u_home / ".gemini").as_posix()
            raw = _snapshot_sqlite_bytes(cp)
            files.append(
                HarnessTransferFile(
                    relpath=rel,
                    content_b64=base64.b64encode(raw).decode("ascii"),
                )
            )
            snapshotted_dbs.add(cp.name)
        except (OSError, sqlite3.Error):
            _log.warning(
                "failed to snapshot agy sqlite db %s; falling back to raw read", cp, exc_info=True
            )

    for cp in candidates:
        if cp.name in snapshotted_dbs or cp.name.endswith(".db-shm"):
            continue
        if cp.name.endswith(".db-wal") and cp.name[:-4] in snapshotted_dbs:
            continue
        try:
            rel = cp.relative_to(u_home / ".gemini").as_posix()
            files.append(
                HarnessTransferFile(
                    relpath=rel,
                    content_b64=base64.b64encode(cp.read_bytes()).decode("ascii"),
                )
            )
        except OSError:
            _log.warning("failed to read agy conv file %s", cp, exc_info=True)


def collect_antigravity_files(
    u_home: Path,
    session_id: str,
    files: list[HarnessTransferFile],
) -> None:
    for dir_name in ("antigravity-cli", "antigravity"):
        agy_root = u_home / ".gemini" / dir_name
        brain_dir = agy_root / "brain" / session_id
        if brain_dir.is_dir():
            for root, _, filenames in os.walk(brain_dir):
                for fn in sorted(filenames):
                    fp = Path(root) / fn
                    try:
                        rel = fp.relative_to(u_home / ".gemini").as_posix()
                        files.append(
                            HarnessTransferFile(
                                relpath=rel,
                                content_b64=base64.b64encode(fp.read_bytes()).decode("ascii"),
                            )
                        )
                    except OSError:
                        _log.warning("failed to read agy brain file %s", fp, exc_info=True)

        conv_dir = agy_root / "conversations"
        if conv_dir.is_dir():
            _collect_conversation_files(u_home, conv_dir, session_id, files)


def _rewrite_metadata_row(
    cur: sqlite3.Cursor,
    db_path: Path,
    raw_row: object,
    *,
    from_cwd: str,
    to_cwd: str,
) -> bool:
    if not _is_row_tuple(raw_row) or len(raw_row) < 2:
        return False
    row_id, blob = raw_row[0], raw_row[1]
    if not isinstance(blob, bytes) or not blob:
        return False
    try:
        rewritten = rewrite_trajectory_metadata_pb(blob, from_cwd=from_cwd, to_cwd=to_cwd)
    except ValueError:
        _log.warning(
            "failed to parse antigravity trajectory_metadata_blob in %s (id=%r)",
            db_path,
            row_id,
            exc_info=True,
        )
        return False
    if rewritten == blob:
        return False
    _ = cur.execute(
        "UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?",
        (rewritten, row_id),
    )
    return True


def _rewrite_conversation_db(db_path: Path, *, from_cwd: str, to_cwd: str) -> None:
    if not db_path.is_file() or not _is_sqlite_file(db_path):
        return
    try:
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            cur = conn.cursor()
            _ = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_metadata_blob'"
            )
            if cur.fetchone() is None:
                return
            _ = cur.execute("SELECT id, data FROM trajectory_metadata_blob")
            raw_rows = object_of(cur.fetchall)
            updated = [
                _rewrite_metadata_row(cur, db_path, raw_row, from_cwd=from_cwd, to_cwd=to_cwd)
                for raw_row in (raw_rows if is_object_list(raw_rows) else [])
            ]
            if any(updated):
                conn.commit()
            _ = cur.execute("PRAGMA journal_mode")
            mode_row = object_of(cur.fetchone)
            if _is_row_tuple(mode_row) and mode_row and str(mode_row[0]).lower() == "wal":
                _ = cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        _log.warning("failed to rewrite antigravity conversation db %s", db_path, exc_info=True)


def _rewrite_summary_db(
    summary_db: Path,
    *,
    session_id: str,
    from_cwd: str,
    to_cwd: str,
) -> None:
    if not session_id or not summary_db.is_file() or not _is_sqlite_file(summary_db):
        return
    try:
        with contextlib.closing(sqlite3.connect(summary_db)) as conn:
            cur = conn.cursor()
            _ = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_summaries'"
            )
            if cur.fetchone() is None:
                return
            _ = cur.execute(
                "SELECT workspace_uris, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                (session_id,),
            )
            row = object_of(cur.fetchone)
            if not _is_row_tuple(row) or len(row) < 2:
                return
            ws_uris, raw_summary = row[0], row[1]
            new_ws_uris: object = (
                rewrite_workspace_uris_json(ws_uris, from_cwd=from_cwd, to_cwd=to_cwd)
                if isinstance(ws_uris, str)
                else ws_uris
            )
            new_raw_summary: object = raw_summary
            if isinstance(raw_summary, bytes) and raw_summary:
                try:
                    new_raw_summary = rewrite_cascade_summary_pb(
                        raw_summary, from_cwd=from_cwd, to_cwd=to_cwd
                    )
                except ValueError:
                    _log.warning(
                        "failed to parse antigravity raw_summary in %s for %s",
                        summary_db,
                        session_id,
                        exc_info=True,
                    )
            if new_ws_uris != ws_uris or new_raw_summary != raw_summary:
                _ = cur.execute(
                    "UPDATE conversation_summaries SET workspace_uris = ?, raw_summary = ? WHERE conversation_id = ?",
                    (new_ws_uris, new_raw_summary, session_id),
                )
                conn.commit()
    except sqlite3.Error:
        _log.warning("failed to rewrite antigravity summary db %s", summary_db, exc_info=True)


def _rewrite_jsonl(text: str, *, from_cwd: str, to_cwd: str) -> str:
    if not from_cwd or from_cwd == to_cwd:
        return text
    from_uri = path_to_file_uri(from_cwd)
    to_uri = path_to_file_uri(to_cwd)
    if from_uri and to_uri and from_uri != to_uri:
        text = text.replace(from_uri, to_uri)
    return text.replace(from_cwd, to_cwd)


def _write_antigravity_file(
    target_gemini_dir: Path,
    file_entry: HarnessTransferFile,
    *,
    wal_relpaths: set[str],
    rewrite_needed: bool,
    from_cwd: str,
    to_cwd: str,
) -> Path | None:
    target_fp = target_gemini_dir / file_entry.relpath
    target_fp.parent.mkdir(parents=True, exist_ok=True)
    content = base64.b64decode(file_entry.content_b64)
    if target_fp.name.endswith(".jsonl"):
        text = content.decode("utf-8", errors="replace")
        if rewrite_needed:
            text = _rewrite_jsonl(text, from_cwd=from_cwd, to_cwd=to_cwd)
        _ = target_fp.write_text(text, encoding="utf-8")
        return None
    if target_fp.name.endswith(".db") and target_fp.parent.name == "conversations":
        with contextlib.suppress(OSError):
            Path(f"{target_fp}-shm").unlink()
        if f"{file_entry.relpath}-wal" not in wal_relpaths:
            with contextlib.suppress(OSError):
                Path(f"{target_fp}-wal").unlink()
        _ = target_fp.write_bytes(content)
        return target_fp
    if (
        target_fp.name.endswith(".pb")
        and target_fp.parent.name == "conversations"
        and rewrite_needed
    ):
        try:
            content = rewrite_trajectory_pb(content, from_cwd=from_cwd, to_cwd=to_cwd)
        except ValueError:
            _log.warning(
                "failed to parse antigravity legacy trajectory pb %s",
                target_fp,
                exc_info=True,
            )
    _ = target_fp.write_bytes(content)
    return None


def apply_antigravity_transfer(
    u_home: Path,
    payload: HarnessTransferPayload,
    from_cwd: str,
    to_cwd: str,
) -> None:
    target_gemini_dir = u_home / ".gemini"
    wal_relpaths = {f.relpath for f in payload.files if f.relpath.endswith(".db-wal")}
    written_dbs: list[Path] = []
    touched_agy_roots: set[Path] = set()
    rewrite_needed = bool(from_cwd and to_cwd and from_cwd != to_cwd)

    for file_entry in payload.files:
        if file_entry.relpath.endswith(".db-shm"):
            continue
        parts = Path(file_entry.relpath).parts
        if parts and parts[0] in ("antigravity-cli", "antigravity"):
            touched_agy_roots.add(target_gemini_dir / parts[0])
        written_db = _write_antigravity_file(
            target_gemini_dir,
            file_entry,
            wal_relpaths=wal_relpaths,
            rewrite_needed=rewrite_needed,
            from_cwd=from_cwd,
            to_cwd=to_cwd,
        )
        if written_db is not None:
            written_dbs.append(written_db)

    if rewrite_needed:
        for db_path in written_dbs:
            _rewrite_conversation_db(db_path, from_cwd=from_cwd, to_cwd=to_cwd)
        session_id = str(payload.session_record.get("session_id") or "")
        for agy_root in sorted(touched_agy_roots):
            _rewrite_summary_db(
                agy_root / "conversation_summaries.db",
                session_id=session_id,
                from_cwd=from_cwd,
                to_cwd=to_cwd,
            )
