# meiva:note
# Packaging, relocation, path rewriting, and restoration of external harness session
# records and transcripts (Claude Code, Antigravity, Codex) across cross-machine transfers.
#
# meiva:link gateway/agents/docs/agent-transfer.md

from __future__ import annotations

import base64
import logging
import os
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path

from base import paths
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.lm.harness import encode_items
from base.lm.harness.registry import FileSessionRegistry, SessionRecord, record_from_payload
from ops.rpc_schemas.transfer import HarnessTransferFile, HarnessTransferPayload

_log = logging.getLogger(__name__)

_CODEX_HISTORY_TABLES = ("thread_items", "thread_turns", "thread_realtime_items")


def claude_project_slug(cwd: str) -> str:
    return re.sub(r"[/\\.]", "-", cwd)


def _collect_claude_files(
    u_home: Path,
    from_cwd: str,
    session_id: str,
    files: list[HarnessTransferFile],
    extra: dict[str, object],
) -> None:
    from_slug = claude_project_slug(from_cwd)
    extra["from_slug"] = from_slug
    claude_projects_dir = u_home / ".claude" / "projects" / from_slug
    transcript_file = claude_projects_dir / f"{session_id}.jsonl"
    if transcript_file.is_file():
        try:
            files.append(
                HarnessTransferFile(
                    relpath=f"projects/{from_slug}/{session_id}.jsonl",
                    content_b64=base64.b64encode(transcript_file.read_bytes()).decode("ascii"),
                )
            )
        except OSError:
            _log.warning("failed to read claude transcript %s", transcript_file, exc_info=True)

    session_sub_dir = claude_projects_dir / session_id
    if session_sub_dir.is_dir():
        for root, _, filenames in os.walk(session_sub_dir):
            for fn in sorted(filenames):
                fp = Path(root) / fn
                try:
                    rel = fp.relative_to(claude_projects_dir).as_posix()
                    files.append(
                        HarnessTransferFile(
                            relpath=f"projects/{from_slug}/{rel}",
                            content_b64=base64.b64encode(fp.read_bytes()).decode("ascii"),
                        )
                    )
                except OSError:
                    _log.warning("failed to read claude session file %s", fp, exc_info=True)


def _collect_antigravity_files(
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
            for cp in sorted(conv_dir.glob(f"{session_id}.*")):
                if cp.is_file():
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


# meiva:note
# Query and pack thread items from Codex thread_history_1.sqlite matching the transferred session id.
def _collect_codex_thread_history(
    th_conn: sqlite3.Connection,
    session_id: str,
) -> dict[str, list[dict[str, object]]]:
    history_data: dict[str, list[dict[str, object]]] = {}
    th_cur = th_conn.cursor()
    for tbl in _CODEX_HISTORY_TABLES:
        _ = th_cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (tbl,),
        )
        if th_cur.fetchone() is not None:
            _ = th_cur.execute(f"SELECT * FROM {tbl} WHERE thread_id = ?", (session_id,))  # noqa: S608
            fetch_res = object_of(th_cur.fetchall)
            if is_object_list(fetch_res) and th_cur.description is not None:
                cols = [str(col[0]) for col in th_cur.description]
                tbl_rows: list[dict[str, object]] = []
                for r in fetch_res:
                    if isinstance(r, (tuple, list)):
                        row_map: dict[str, object] = {
                            col_name: r[col_i] for col_i, col_name in enumerate(cols)
                        }
                        tbl_rows.append(row_map)
                if tbl_rows:
                    history_data[tbl] = tbl_rows
    return history_data


def _query_codex_thread_row(sqlite_path: Path, session_id: str) -> dict[str, object] | None:
    if not sqlite_path.is_file():
        return None
    try:
        with sqlite3.connect(sqlite_path) as conn:
            cur = conn.cursor()
            _ = cur.execute("SELECT * FROM threads WHERE id = ?", (session_id,))
            row_raw = object_of(cur.fetchone)
            if isinstance(row_raw, (tuple, list)) and cur.description is not None:
                return {str(col[0]): row_raw[idx] for idx, col in enumerate(cur.description)}
    except Exception:
        _log.warning("failed to query codex state_5.sqlite for %s", session_id, exc_info=True)
    return None


def _find_codex_rollout(
    codex_root: Path, session_id: str, thread_row: dict[str, object] | None
) -> Path | None:
    if thread_row is not None:
        rollout_val = thread_row.get("rollout_path")
        if isinstance(rollout_val, str) and rollout_val:
            candidate = Path(rollout_val)
            if candidate.is_file():
                return candidate
    sessions_dir = codex_root / "sessions"
    if sessions_dir.is_dir():
        candidates = sorted(sessions_dir.glob(f"**/rollout-*{session_id}*"), reverse=True)
        if candidates and candidates[0].is_file():
            return candidates[0]
    return None


def _pack_codex_rollout(
    codex_root: Path,
    rollout_path: Path,
    files: list[HarnessTransferFile],
    extra: dict[str, object],
) -> None:
    try:
        rel_rollout = rollout_path.relative_to(codex_root).as_posix()
    except ValueError:
        rel_rollout = f"sessions/{rollout_path.name}"
    extra["rel_rollout"] = rel_rollout
    try:
        files.append(
            HarnessTransferFile(
                relpath=rel_rollout,
                content_b64=base64.b64encode(rollout_path.read_bytes()).decode("ascii"),
            )
        )
    except OSError:
        _log.warning("failed to read codex rollout %s", rollout_path, exc_info=True)


def _collect_codex_index_lines(index_path: Path, session_id: str) -> list[str]:
    if not index_path.is_file():
        return []
    try:
        return [
            line
            for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if session_id in line
        ]
    except Exception:
        _log.warning("failed to read codex session_index.jsonl", exc_info=True)
        return []


def _collect_codex_history_if_present(
    th_sqlite: Path, session_id: str
) -> dict[str, list[dict[str, object]]]:
    if not th_sqlite.is_file():
        return {}
    try:
        with sqlite3.connect(th_sqlite) as th_conn:
            return _collect_codex_thread_history(th_conn, session_id)
    except Exception:
        _log.warning("failed to read codex thread_history_1.sqlite", exc_info=True)
        return {}


def _collect_codex_files(
    u_home: Path,
    session_id: str,
    files: list[HarnessTransferFile],
    extra: dict[str, object],
) -> None:
    codex_root = u_home / ".codex"
    thread_row = _query_codex_thread_row(codex_root / "state_5.sqlite", session_id)
    if thread_row is not None:
        extra["thread_row"] = thread_row

    rollout_path = _find_codex_rollout(codex_root, session_id, thread_row)
    if rollout_path is not None:
        _pack_codex_rollout(codex_root, rollout_path, files, extra)

    idx_lines = _collect_codex_index_lines(codex_root / "session_index.jsonl", session_id)
    if idx_lines:
        extra["session_index_lines"] = idx_lines

    history = _collect_codex_history_if_present(codex_root / "thread_history_1.sqlite", session_id)
    if history:
        extra["thread_history"] = history


# meiva:note
# Collect every external harness session record of the agent
# ($MEIVA_HOME/harness/sessions/{agent_id}.{harness}.json) with the transcript files of its CLI
# from the user's home directory, one immutable `HarnessTransferPayload` per harness, for the
# workspace transfer bundle. A record is carried only when its transcript was found: a pointer to a
# transcript that did not come along would resume nothing. The other harnesses, those with no
# transcript support (`_TRANSFERABLE` names the ones with a collector) or whose transcript is
# missing, are returned by name so the transfer can say they stayed behind, and their records stay
# where they are.
def collect_harness_transfers(
    *,
    home: Path,
    agent_id: int,
    user_home: Path | None = None,
) -> tuple[list[HarnessTransferPayload], list[str]]:
    try:
        records = FileSessionRegistry(paths.harness_sessions_dir(home=home)).records(agent_id)
    except (OSError, ValueError, TypeError):
        _log.warning("failed to read session records of agent %s", agent_id, exc_info=True)
        return [], []
    u_home = user_home if user_home is not None else home.parent
    carried: list[HarnessTransferPayload] = []
    left_behind: list[str] = []
    for record in records:
        payload = _collect_one(record, home=home, u_home=u_home)
        if payload is None:
            left_behind.append(record.harness)
        else:
            carried.append(payload)
    return carried, left_behind


_TRANSFERABLE = ("claude_code", "antigravity", "codex")


def _collect_one(
    record: SessionRecord, *, home: Path, u_home: Path
) -> HarnessTransferPayload | None:
    if record.harness not in _TRANSFERABLE or not record.session_id:
        return None
    session_record: dict[str, object] = {
        **asdict(record),
        "background": encode_items(record.background),
    }
    from_cwd = record.cwd or str(paths.workspace_path(home, record.agent_id))
    files: list[HarnessTransferFile] = []
    extra: dict[str, object] = {}
    if record.harness == "claude_code":
        _collect_claude_files(u_home, from_cwd, record.session_id, files, extra)
    elif record.harness == "antigravity":
        _collect_antigravity_files(u_home, record.session_id, files)
    else:
        _collect_codex_files(u_home, record.session_id, files, extra)
    if not files and not extra:
        return None
    return HarnessTransferPayload(
        harness=record.harness, session_record=session_record, files=files, extra=extra
    )


def _apply_claude_transfer(
    u_home: Path,
    payload: HarnessTransferPayload,
    from_cwd: str,
    to_cwd: str,
) -> None:
    from_slug = str(payload.extra.get("from_slug") or claude_project_slug(from_cwd))
    to_slug = claude_project_slug(to_cwd)
    target_claude_dir = u_home / ".claude"

    for f in payload.files:
        rel = f.relpath
        prefix = f"projects/{from_slug}/"
        if rel.startswith(prefix):
            rel = f"projects/{to_slug}/" + rel[len(prefix) :]
        target_fp = target_claude_dir / rel
        target_fp.parent.mkdir(parents=True, exist_ok=True)
        content = base64.b64decode(f.content_b64)
        if target_fp.name.endswith(".jsonl"):
            text = content.decode("utf-8", errors="replace")
            if from_cwd:
                text = text.replace(from_cwd, to_cwd)
            _ = target_fp.write_text(text, encoding="utf-8")
        else:
            _ = target_fp.write_bytes(content)


def _apply_antigravity_transfer(
    u_home: Path,
    payload: HarnessTransferPayload,
    from_cwd: str,
    to_cwd: str,
) -> None:
    target_gemini_dir = u_home / ".gemini"
    for f in payload.files:
        target_fp = target_gemini_dir / f.relpath
        target_fp.parent.mkdir(parents=True, exist_ok=True)
        content = base64.b64decode(f.content_b64)
        if target_fp.name.endswith(".jsonl"):
            text = content.decode("utf-8", errors="replace")
            if from_cwd:
                text = text.replace(from_cwd, to_cwd)
            _ = target_fp.write_text(text, encoding="utf-8")
        else:
            _ = target_fp.write_bytes(content)


def _apply_codex_session_index(target_codex_dir: Path, raw_lines: list[object]) -> None:
    index_fp = target_codex_dir / "session_index.jsonl"
    try:
        index_fp.parent.mkdir(parents=True, exist_ok=True)
        existing_lines: set[str] = (
            set(index_fp.read_text(encoding="utf-8").splitlines())
            if index_fp.is_file()
            else set[str]()
        )
        lines_to_add: list[str] = [
            line for line in raw_lines if isinstance(line, str) and line not in existing_lines
        ]
        if lines_to_add:
            with index_fp.open("a", encoding="utf-8") as stream:
                for line in lines_to_add:
                    _ = stream.write(f"{line}\n")
    except Exception:
        _log.warning("failed to append to codex session_index.jsonl", exc_info=True)


# meiva:note
# Upsert the transferred Codex thread row into SQLite state_5.sqlite under parameterized values.
def _apply_codex_thread_row(
    target_codex_dir: Path,
    raw_thread_row: dict[str, object],
    to_cwd: str,
    new_rollout_path: str | None,
) -> None:
    sqlite_path = target_codex_dir / "state_5.sqlite"
    if not sqlite_path.is_file():
        return
    try:
        with sqlite3.connect(sqlite_path) as conn:
            cur = conn.cursor()
            _ = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'")
            if cur.fetchone() is not None:
                _ = cur.execute("SELECT * FROM threads WHERE 1=0")
                cols: set[str] = (
                    {str(col[0]) for col in cur.description}
                    if cur.description is not None
                    else set[str]()
                )
                row_dict: dict[str, object] = dict(raw_thread_row)
                row_dict["cwd"] = to_cwd
                if new_rollout_path:
                    row_dict["rollout_path"] = new_rollout_path
                valid_row: dict[str, object] = {k: v for k, v in row_dict.items() if k in cols}
                col_names: list[str] = list(valid_row.keys())
                if col_names:
                    placeholders = ", ".join("?" for _ in col_names)
                    sql = f"INSERT OR REPLACE INTO threads ({', '.join(col_names)}) VALUES ({placeholders})"  # noqa: S608
                    params: list[object] = [valid_row[k] for k in col_names]
                    _ = cur.execute(sql, params)
                    conn.commit()
    except Exception:
        _log.warning("failed to restore codex state_5.sqlite thread row", exc_info=True)


# meiva:note
# Insert restored rows into a Codex thread history table under SQLite parameter bindings.
def _insert_history_rows(
    th_cur: sqlite3.Cursor,
    tbl: str,
    rows: list[object],
) -> None:
    _ = th_cur.execute(f"SELECT * FROM {tbl} WHERE 1=0")  # noqa: S608
    if th_cur.description is None:
        return
    th_cols: set[str] = {str(col[0]) for col in th_cur.description}
    for r in rows:
        if not is_object_dict(r):
            continue
        valid_r: dict[str, object] = {k: v for k, v in r.items() if k in th_cols}
        th_col_names = list(valid_r.keys())
        if th_col_names:
            placeholders = ", ".join("?" for _ in th_col_names)
            sql = (
                f"INSERT OR REPLACE INTO {tbl} ({', '.join(th_col_names)}) VALUES ({placeholders})"  # noqa: S608
            )
            th_params: list[object] = [valid_r[k] for k in th_col_names]
            _ = th_cur.execute(sql, th_params)


# meiva:note
# Restore Codex thread history items into SQLite thread_history_1.sqlite on the target host.
def _apply_codex_thread_history(
    target_codex_dir: Path,
    raw_history: dict[str, object],
) -> None:
    th_sqlite = target_codex_dir / "thread_history_1.sqlite"
    if not th_sqlite.is_file():
        return
    try:
        with sqlite3.connect(th_sqlite) as th_conn:
            th_cur = th_conn.cursor()
            for tbl, rows in raw_history.items():
                if not (is_object_list(rows) and tbl in _CODEX_HISTORY_TABLES):
                    continue
                _ = th_cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                    (tbl,),
                )
                if th_cur.fetchone() is not None:
                    _insert_history_rows(th_cur, tbl, rows)
            th_conn.commit()
    except Exception:
        _log.warning("failed to restore codex thread_history_1.sqlite", exc_info=True)


def _apply_codex_transfer(
    u_home: Path,
    payload: HarnessTransferPayload,
    from_cwd: str,
    to_cwd: str,
) -> None:
    target_codex_dir = u_home / ".codex"
    new_rollout_path: str | None = None

    for f in payload.files:
        target_fp = target_codex_dir / f.relpath
        target_fp.parent.mkdir(parents=True, exist_ok=True)
        content = base64.b64decode(f.content_b64)
        if target_fp.name.endswith(".jsonl"):
            text = content.decode("utf-8", errors="replace")
            if from_cwd:
                text = text.replace(from_cwd, to_cwd)
            _ = target_fp.write_text(text, encoding="utf-8")
        else:
            _ = target_fp.write_bytes(content)
        if f.relpath == payload.extra.get("rel_rollout"):
            new_rollout_path = str(target_fp)

    raw_lines = payload.extra.get("session_index_lines")
    if is_object_list(raw_lines):
        _apply_codex_session_index(target_codex_dir, raw_lines)

    raw_thread_row = payload.extra.get("thread_row")
    if is_object_dict(raw_thread_row):
        _apply_codex_thread_row(target_codex_dir, raw_thread_row, to_cwd, new_rollout_path)

    raw_history = payload.extra.get("thread_history")
    if is_object_dict(raw_history):
        _apply_codex_thread_history(target_codex_dir, raw_history)


def _resolve_effective_agent_id(agent_id: int | None, session_record: dict[str, object]) -> int:
    if agent_id is not None:
        return agent_id
    rec_id = session_record.get("agent_id")
    if isinstance(rec_id, int):
        return rec_id
    if isinstance(rec_id, str) and rec_id.isdigit():
        return int(rec_id)
    return 0


def _dispatch_harness_apply(
    harness: str,
    u_home: Path,
    payload: HarnessTransferPayload,
    from_cwd: str,
    to_cwd: str,
) -> None:
    match harness:
        case "claude_code":
            _apply_claude_transfer(u_home, payload, from_cwd, to_cwd)
        case "antigravity":
            _apply_antigravity_transfer(u_home, payload, from_cwd, to_cwd)
        case "codex":
            _apply_codex_transfer(u_home, payload, from_cwd, to_cwd)
        case _:
            pass


# meiva:note
# Restore an external harness session record and its transcript files on `to_machine`,
# rewriting workspace paths in `cwd`, directory slugs, and index references so the external
# CLI seamlessly resumes history from its new location.
def apply_harness_transfer(
    *,
    home: Path,
    payload: HarnessTransferPayload,
    to_machine: str = "",
    agent_id: int | None = None,
    user_home: Path | None = None,
) -> None:
    _ = to_machine
    effective_agent_id = _resolve_effective_agent_id(agent_id, payload.session_record)
    if effective_agent_id <= 0:
        return

    to_cwd = str(paths.workspace_path(home, effective_agent_id))
    from_cwd = str(payload.session_record.get("cwd") or "")
    u_home = user_home if user_home is not None else home.parent

    _dispatch_harness_apply(payload.harness, u_home, payload, from_cwd, to_cwd)

    record = dict(payload.session_record)
    record["agent_id"] = effective_agent_id
    record["cwd"] = to_cwd
    FileSessionRegistry(paths.harness_sessions_dir(home=home)).save(
        record_from_payload(record, source="the transfer bundle")
    )
