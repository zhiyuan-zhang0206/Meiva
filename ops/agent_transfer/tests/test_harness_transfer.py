# meiva:note
# Comprehensive tests for external harness transcript and session relocation
# across cross-machine workspace transfer (Claude Code, Antigravity, Codex).
#
# meiva:link gateway/agents/docs/agent-transfer.md

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from base import paths
from base.lm.harness.registry import FileSessionRegistry
from ops.agent_transfer.archive_io import pack_workspace, stage_and_activate_workspace
from ops.agent_transfer.budget import WorkspaceTransferLimits
from ops.agent_transfer.harness_transfer import (
    apply_harness_transfer,
    claude_project_slug,
    collect_harness_transfer,
)
from ops.agent_transfer.operations import finalize_transfer_op
from ops.rpc_schemas.transfer import WorkspaceTransferFinalizePayload

_LIMITS = WorkspaceTransferLimits(max_files=1000, max_bytes=16 * 1024 * 1024)


def test_claude_project_slug_derivation() -> None:
    assert (
        claude_project_slug("/home/zzy/.meiva/workspaces/7180")
        == "-home-zzy--meiva-workspaces-7180"
    )
    assert (
        claude_project_slug("/Users/zhiyuanzhang/.meiva/workspaces/7042")
        == "-Users-zhiyuanzhang--meiva-workspaces-7042"
    )


def test_claude_code_transfer_collect_and_apply(tmp_path: Path) -> None:
    src_home = tmp_path / "src_home"
    dst_home = tmp_path / "dst_home"
    src_user = tmp_path / "src_user"
    dst_user = tmp_path / "dst_user"

    agent_id = 7180
    session_id = "946e042f-2b63-45e4-97a9-6c887e8c0ecf"
    src_cwd = str(paths.workspace_path(src_home, agent_id))
    dst_cwd = str(paths.workspace_path(dst_home, agent_id))

    session_file = paths.harness_sessions_dir(home=src_home) / f"{agent_id}.claude_code.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "cwd": src_cwd,
                "harness": "claude_code",
                "model": "claude-sonnet-5-5",
                "resumes": 3,
                "created_at": "2026-10-28T13:15:26+00:00",
                "updated_at": "2026-10-28T20:37:10+00:00",
            }
        ),
        encoding="utf-8",
    )

    src_slug = claude_project_slug(src_cwd)
    transcript_dir = src_user / ".claude" / "projects" / src_slug
    transcript_dir.mkdir(parents=True, exist_ok=True)
    sample_jsonl = (
        f'{{"type":"user","content":"hello","cwd":"{src_cwd}"}}\n'
        f'{{"type":"assistant","content":"hi there"}}\n'
    )
    (transcript_dir / f"{session_id}.jsonl").write_text(sample_jsonl, encoding="utf-8")

    payload = collect_harness_transfer(home=src_home, agent_id=agent_id, user_home=src_user)
    assert payload is not None
    assert payload.harness == "claude_code"
    assert payload.session_record["session_id"] == session_id
    assert len(payload.files) == 1
    assert payload.files[0].relpath == f"projects/{src_slug}/{session_id}.jsonl"

    apply_harness_transfer(
        home=dst_home, payload=payload, to_machine="dst_runner", user_home=dst_user
    )

    dst_session_file = paths.harness_sessions_dir(home=dst_home) / f"{agent_id}.claude_code.json"
    assert dst_session_file.is_file()
    dst_record = json.loads(dst_session_file.read_text(encoding="utf-8"))
    assert dst_record["cwd"] == dst_cwd
    assert dst_record["session_id"] == session_id
    assert dst_record["harness"] == "claude_code"

    dst_slug = claude_project_slug(dst_cwd)
    dst_transcript_file = dst_user / ".claude" / "projects" / dst_slug / f"{session_id}.jsonl"
    assert dst_transcript_file.is_file()
    dst_content = dst_transcript_file.read_text(encoding="utf-8")
    assert dst_cwd in dst_content
    assert src_cwd not in dst_content
    assert "hi there" in dst_content


def test_antigravity_transfer_collect_and_apply(tmp_path: Path) -> None:
    src_home = tmp_path / "src_home"
    dst_home = tmp_path / "dst_home"
    src_user = tmp_path / "src_user"
    dst_user = tmp_path / "dst_user"

    agent_id = 7182
    session_id = "e4477f1d-2d55-4bc8-b4af-13c86d7dd57f"
    src_cwd = str(paths.workspace_path(src_home, agent_id))
    dst_cwd = str(paths.workspace_path(dst_home, agent_id))

    session_file = paths.harness_sessions_dir(home=src_home) / f"{agent_id}.antigravity.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "cwd": src_cwd,
                "harness": "antigravity",
                "model": "gemini-4-argon-low",
                "resumes": 1,
                "created_at": "2026-10-28T13:15:32+00:00",
                "updated_at": "2026-10-28T20:33:11+00:00",
            }
        ),
        encoding="utf-8",
    )

    brain_dir = (
        src_user
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / session_id
        / ".system_generated"
        / "logs"
    )
    brain_dir.mkdir(parents=True, exist_ok=True)
    (brain_dir / "transcript.jsonl").write_text(
        f'{{"type":"USER_INPUT","content":"agy hello","cwd":"{src_cwd}"}}\n',
        encoding="utf-8",
    )
    conv_dir = src_user / ".gemini" / "antigravity-cli" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    (conv_dir / f"{session_id}.pb").write_bytes(b"\x08\x01\x12\x04test")

    payload = collect_harness_transfer(home=src_home, agent_id=agent_id, user_home=src_user)
    assert payload is not None
    assert payload.harness == "antigravity"
    assert len(payload.files) >= 2

    apply_harness_transfer(
        home=dst_home, payload=payload, to_machine="dst_runner", user_home=dst_user
    )

    dst_session_file = paths.harness_sessions_dir(home=dst_home) / f"{agent_id}.antigravity.json"
    assert dst_session_file.is_file()
    dst_record = json.loads(dst_session_file.read_text(encoding="utf-8"))
    assert dst_record["cwd"] == dst_cwd
    assert dst_record["session_id"] == session_id

    dst_brain = (
        dst_user
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / session_id
        / ".system_generated"
        / "logs"
    )
    assert (dst_brain / "transcript.jsonl").is_file()
    dst_transcript = (dst_brain / "transcript.jsonl").read_text(encoding="utf-8")
    assert dst_cwd in dst_transcript
    assert (
        dst_user / ".gemini" / "antigravity-cli" / "conversations" / f"{session_id}.pb"
    ).is_file()
    assert (
        dst_user / ".gemini" / "antigravity-cli" / "conversations" / f"{session_id}.pb"
    ).read_bytes() == b"\x08\x01\x12\x04test"


def test_codex_transfer_collect_and_apply(tmp_path: Path) -> None:
    src_home = tmp_path / "src_home"
    dst_home = tmp_path / "dst_home"
    src_user = tmp_path / "src_user"
    dst_user = tmp_path / "dst_user"

    agent_id = 7181
    session_id = "01a182a6-b411-7c73-a6b1-02647427ab48"
    src_cwd = str(paths.workspace_path(src_home, agent_id))
    dst_cwd = str(paths.workspace_path(dst_home, agent_id))

    session_file = paths.harness_sessions_dir(home=src_home) / f"{agent_id}.codex.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "cwd": src_cwd,
                "harness": "codex",
                "model": "gpt-6-astra",
                "resumes": 1,
                "created_at": "2026-10-28T13:15:27+00:00",
                "updated_at": "2026-10-28T20:33:10+00:00",
            }
        ),
        encoding="utf-8",
    )

    rollout_dir = src_user / ".codex" / "sessions" / "2026" / "10" / "28"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout_file = rollout_dir / f"rollout-2026-10-28T21-15-27-{session_id}.jsonl"
    rollout_file.write_text(
        f'{{"type":"session_meta","payload":{{"id":"{session_id}","cwd":"{src_cwd}"}}}}\n',
        encoding="utf-8",
    )

    sqlite_path = src_user / ".codex" / "state_5.sqlite"
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT, model TEXT)"
        )
        conn.execute(
            "INSERT INTO threads (id, rollout_path, cwd, title, model) VALUES (?, ?, ?, ?, ?)",
            (session_id, str(rollout_file), src_cwd, "Test Thread", "gpt-6-astra"),
        )
        conn.commit()

    (src_user / ".codex" / "session_index.jsonl").write_text(
        f'{{"id":"{session_id}","thread_name":"Test Thread","updated_at":"2026-10-28T20:33:10Z"}}\n',
        encoding="utf-8",
    )

    dst_sqlite_path = dst_user / ".codex" / "state_5.sqlite"
    dst_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(dst_sqlite_path) as conn:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT, model TEXT)"
        )
        conn.commit()

    payload = collect_harness_transfer(home=src_home, agent_id=agent_id, user_home=src_user)
    assert payload is not None
    assert payload.harness == "codex"
    assert "thread_row" in payload.extra
    assert "session_index_lines" in payload.extra

    apply_harness_transfer(
        home=dst_home, payload=payload, to_machine="dst_runner", user_home=dst_user
    )

    dst_session_file = paths.harness_sessions_dir(home=dst_home) / f"{agent_id}.codex.json"
    assert dst_session_file.is_file()
    dst_record = json.loads(dst_session_file.read_text(encoding="utf-8"))
    assert dst_record["cwd"] == dst_cwd

    with sqlite3.connect(dst_sqlite_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, rollout_path, cwd, title FROM threads WHERE id = ?", (session_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == session_id
        assert row[2] == dst_cwd
        assert dst_cwd in (
            dst_user
            / ".codex"
            / "sessions"
            / "2026"
            / "10"
            / "28"
            / f"rollout-2026-10-28T21-15-27-{session_id}.jsonl"
        ).read_text(encoding="utf-8")

    dst_index = (dst_user / ".codex" / "session_index.jsonl").read_text(encoding="utf-8")
    assert session_id in dst_index


def test_bundle_pack_and_activate_with_harness_transfer(tmp_path: Path) -> None:
    src_home = tmp_path / "source-home"
    dst_home = tmp_path / "target-home"
    src_user = tmp_path / "source-user"
    dst_user = tmp_path / "target-user"

    agent_id = 4242
    session_id = "sess-transfer-e2e"
    src_ws = paths.workspace_path(src_home, agent_id)
    src_ws.mkdir(parents=True)
    (src_ws / "code.py").write_text("print('hello')\n", encoding="utf-8")

    session_file = paths.harness_sessions_dir(home=src_home) / f"{agent_id}.claude_code.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "cwd": str(src_ws),
                "harness": "claude_code",
                "model": "claude-sonnet-5-5",
                "resumes": 0,
                "created_at": "2026-11-01T00:00:00Z",
                "updated_at": "2026-11-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    src_slug = claude_project_slug(str(src_ws))
    src_transcript_dir = src_user / ".claude" / "projects" / src_slug
    src_transcript_dir.mkdir(parents=True, exist_ok=True)
    (src_transcript_dir / f"{session_id}.jsonl").write_text(
        f'{{"type":"say","cwd":"{src_ws}"}}\n', encoding="utf-8"
    )

    harness_payload = collect_harness_transfer(home=src_home, agent_id=agent_id, user_home=src_user)
    assert harness_payload is not None

    manifest, bundle = pack_workspace(
        home=src_home,
        agent_id=agent_id,
        transfer_id="11" * 16,
        source_machine="box-a",
        killed_shell_sessions=[],
        limits=_LIMITS,
        harness_transfer=harness_payload,
    )
    assert manifest.harness_transfer is not None

    activated = stage_and_activate_workspace(
        home=dst_home,
        agent_id=agent_id,
        transfer_id="11" * 16,
        bundle_bytes=bundle,
        limits=_LIMITS,
    )
    assert activated.files_count == 1

    apply_harness_transfer(
        home=dst_home,
        payload=manifest.harness_transfer,
        to_machine="box-b",
        user_home=dst_user,
    )

    dst_ws = paths.workspace_path(dst_home, agent_id)
    assert (dst_ws / "code.py").read_text(encoding="utf-8") == "print('hello')\n"

    dst_session = paths.harness_sessions_dir(home=dst_home) / f"{agent_id}.claude_code.json"
    assert dst_session.is_file()
    record = json.loads(dst_session.read_text(encoding="utf-8"))
    assert record["cwd"] == str(dst_ws)

    _ = finalize_transfer_op(
        WorkspaceTransferFinalizePayload(
            agent_id=agent_id,
            transfer_id="11" * 16,
            role="source",
        ),
        local_machine="box-a",
        home=src_home,
    )
    assert FileSessionRegistry(paths.harness_sessions_dir(home=src_home)).records(agent_id) == []
