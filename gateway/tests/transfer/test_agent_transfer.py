# meiva:note
# End-to-end integration tests for `POST /api/agents/{agent_id}/transfer` across two real
# `local_runner` `agent-ops` daemons with isolated homes, real Postgres, and a dialable loopback
# gateway bundle endpoint. Zero monkeypatching.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import uuid
from collections.abc import Generator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from base import paths
from base.cluster.machine import MachineFacts
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from gateway.agents.transfer.router import CutoverConflictError, commit_cutover_blocking
from gateway.agents.transfer.transfer_plan import AgentTransferSnapshot
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)
from gateway.tests.support.runner_ops.local_runner import local_runner
from gateway.tests.upload_delivery.support import serving_gateway
from ops.agent_transfer.archive_io import (
    abort_target_workspace,
    pack_workspace,
    stage_and_activate_workspace,
)
from ops.rpc_schemas import TransferAgentRequest


@dataclass(frozen=True)
class TwoRunnerCluster:
    client: GatewayTestClient
    home_a: Path
    home_b: Path


@pytest.fixture
def two_runners(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    database: Database,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> Generator[TwoRunnerCluster]:
    home_a = tmp_path / "home-runner-a"
    home_b = tmp_path / "home-runner-b"
    home_a.mkdir()
    home_b.mkdir()

    machine_a = MachineFacts.of(
        name="runner-a",
        role=frozenset({"agent-runner"}),
        host="127.0.0.1",
        description="Source runner",
    )
    machine_b = MachineFacts.of(
        name="runner-b",
        role=frozenset({"agent-runner"}),
        host="127.0.0.1",
        description="Target runner",
    )

    with (
        gateway_test_client(
            config=gateway_test_config(upload_recovery_enabled=False),
            raise_server_exceptions=False,
        ) as client,
        serving_gateway(client, client.app.state.db_pool) as loopback_gateway_url,
    ):
        access = replace(client.app.state.gateway_access, gateway_url=loopback_gateway_url)
        client.app.state.gateway_access = access
        with (
            local_runner(
                db_conn,
                database=database,
                authority=config_authority,
                catalog=model_catalog,
                machine=machine_a,
                host=fake_host(),
                image=LOADED_IMAGE,
                unit_home=home_a,
                gateway_access=access,
            ),
            local_runner(
                db_conn,
                database=database,
                authority=config_authority,
                catalog=model_catalog,
                machine=machine_b,
                host=fake_host(),
                image=LOADED_IMAGE,
                unit_home=home_b,
                gateway_access=access,
            ),
        ):
            yield TwoRunnerCluster(client=client, home_a=home_a, home_b=home_b)


def _seed_native_agent(
    conn: psycopg.Connection,
    *,
    machine: str = "runner-a",
    status: str = "idling",
    harness: str = "meiva",
    with_settled_incarnation: bool = True,
) -> int:
    agent_id = create_agent(conn)
    owner = uuid.uuid4() if with_settled_incarnation else None
    gen = uuid.uuid4() if with_settled_incarnation else None
    admitted_at = datetime(2026, 11, 1, 1, 0, tzinfo=UTC) if with_settled_incarnation else None
    term_source = "exit" if status == "terminated" else None
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine, harness, runtime_owner, runtime_generation, "
        "last_admission_at, session_index, termination_source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 2, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "status = EXCLUDED.status, machine = EXCLUDED.machine, harness = EXCLUDED.harness, "
        "runtime_owner = EXCLUDED.runtime_owner, runtime_generation = EXCLUDED.runtime_generation, "
        "last_admission_at = EXCLUDED.last_admission_at, session_index = EXCLUDED.session_index, "
        "termination_source = EXCLUDED.termination_source",
        (agent_id, status, machine, harness, owner, gen, admitted_at, term_source),
    )
    conn.commit()
    return agent_id


def _assert_transferred_db_records(db_conn: psycopg.Connection, agent_id: int) -> None:
    meta_row = db_conn.execute(
        "SELECT machine, status, runtime_owner, runtime_generation FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert meta_row == ("runner-b", "idling", None, None)

    open_pages = db_conn.execute(
        "SELECT count(*) FROM agent_pages WHERE agent_id = %s AND closed_at IS NULL",
        (agent_id,),
    ).fetchone()
    shell_ttls = db_conn.execute(
        "SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    assert (open_pages, shell_ttls) == ((0,), (0,))

    inbound = db_conn.execute(
        "SELECT kind, source, content, payload FROM inbound_messages "
        "WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert inbound is not None
    kind, source, content, payload = inbound
    assert (kind, source, payload) == (
        "system_note",
        "user",
        {"note_tag": "context", "passive": True},
    )
    assert "from machine 'runner-a' to 'runner-b'" in content
    assert "Relocating Output owner to company-mini" in content

    audit = db_conn.execute(
        "SELECT event_name, source, attributes FROM audit_events "
        "WHERE agent_id = %s AND event_name = 'transfer'",
        (agent_id,),
    ).fetchone()
    assert audit is not None
    assert (audit[0], audit[1]) == ("transfer", "user")
    assert (
        audit[2]["from_machine"],
        audit[2]["to_machine"],
        audit[2]["closed_pages"],
    ) == ("runner-a", "runner-b", ["board"])


def test_transfer_idling_agent_moves_workspace_clears_incarnation_and_records_passive_note(
    two_runners: TwoRunnerCluster,
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _seed_native_agent(db_conn, machine="runner-a", status="idling")

    ws_a = paths.workspace_path(two_runners.home_a, agent_id)
    (ws_a / "reports").mkdir(parents=True)
    (ws_a / "to_user.md").write_text("# Output Owner\nAll green.\n", encoding="utf-8")
    tool = ws_a / "reports" / "collect.sh"
    tool.write_text("#!/bin/sh\necho collected\n", encoding="utf-8")
    tool.chmod(0o700)
    (ws_a / "collect_link").symlink_to("reports/collect.sh")

    db_conn.execute(
        "INSERT INTO agent_pages (agent_id, name, port, title) VALUES (%s, 'board', 19400, 'Board')",
        (agent_id,),
    )
    db_conn.execute(
        "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
        "VALUES (%s, 0, now() + interval '1 hour')",
        (agent_id,),
    )
    db_conn.commit()

    resp = two_runners.client.post(
        f"/api/agents/{agent_id}/transfer",
        json={
            "to_machine": "runner-b",
            "source": "user",
            "message": "Relocating Output owner to company-mini",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (
        body["agent_id"],
        body["from_machine"],
        body["to_machine"],
        body["agent_status"],
        body["files_transferred"],
        body["closed_pages"],
    ) == (agent_id, "runner-a", "runner-b", "idling", 2, ["board"])

    ws_b = paths.workspace_path(two_runners.home_b, agent_id)
    assert (ws_b / "to_user.md").read_text(encoding="utf-8") == "# Output Owner\nAll green.\n"
    assert (ws_b / "reports" / "collect.sh").stat().st_mode & 0o777 == 0o700
    assert str((ws_b / "collect_link").readlink()) == "reports/collect.sh"
    assert not ws_a.exists()

    _assert_transferred_db_records(db_conn, agent_id)


def test_transfer_terminated_agent_without_workspace_preserves_terminated_status(
    two_runners: TwoRunnerCluster,
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _seed_native_agent(db_conn, machine="runner-a", status="terminated")

    resp = two_runners.client.post(
        f"/api/agents/{agent_id}/transfer",
        json={"to_machine": "runner-b"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_status"] == "terminated"
    assert resp.json()["files_transferred"] == 0

    meta_row = db_conn.execute(
        "SELECT machine, status, termination_source, runtime_owner FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert meta_row == ("runner-b", "terminated", "exit", None)
    assert paths.workspace_path(two_runners.home_b, agent_id).is_dir()


def test_transfer_refuses_running_external_and_impersonated_agents(
    two_runners: TwoRunnerCluster,
    db_conn: psycopg.Connection,
) -> None:
    running_id = _seed_native_agent(db_conn, machine="runner-a", status="running")
    ws_running = paths.workspace_path(two_runners.home_a, running_id)
    ws_running.mkdir(parents=True)
    (ws_running / "keep.txt").write_text("still running", encoding="utf-8")

    r_run = two_runners.client.post(
        f"/api/agents/{running_id}/transfer",
        json={"to_machine": "runner-b"},
    )
    assert r_run.status_code == 409
    assert "currently running" in r_run.json()["detail"]
    assert (ws_running / "keep.txt").read_text(encoding="utf-8") == "still running"
    assert not paths.workspace_path(two_runners.home_b, running_id).exists()

    codex_id = _seed_native_agent(db_conn, machine="runner-a", status="idling", harness="codex")
    r_ext = two_runners.client.post(
        f"/api/agents/{codex_id}/transfer",
        json={"to_machine": "runner-b"},
    )
    assert r_ext.status_code == 409 and "external harness" in r_ext.json()["detail"]

    imp_id = _seed_native_agent(db_conn, machine="runner-a", status="idling")
    now = datetime.now(tz=UTC)
    imp_row = db_conn.execute(
        "INSERT INTO agent_impersonations "
        "(id, agent_id, session_id, source, machine, status, ttl_seconds, expires_at) "
        "VALUES (%s, %s, 0, 'user', 'runner-a', 'active', 3600, %s) RETURNING session_id",
        (uuid.uuid4(), imp_id, now + timedelta(hours=1)),
    ).fetchone()
    assert imp_row is not None
    session_id = int(imp_row[0])
    db_conn.commit()

    r_imp = two_runners.client.post(
        f"/api/agents/{imp_id}/transfer",
        json={"to_machine": "runner-b"},
    )
    assert r_imp.status_code == 409 and f"session {session_id}" in r_imp.json()["detail"]

    db_conn.execute(
        "UPDATE agent_impersonations SET status = 'released', ended_at = now(), "
        "handoff_applied_at = NULL WHERE agent_id = %s AND session_id = %s",
        (imp_id, session_id),
    )
    db_conn.commit()
    r_unsettled = two_runners.client.post(
        f"/api/agents/{imp_id}/transfer",
        json={"to_machine": "runner-b"},
    )
    assert (
        r_unsettled.status_code == 409
        and "unsettled impersonation handoff" in r_unsettled.json()["detail"]
    )


def test_transfer_refuses_invalid_or_paused_targets(
    two_runners: TwoRunnerCluster,
    db_conn: psycopg.Connection,
) -> None:
    settled_id = _seed_native_agent(db_conn, machine="runner-a", status="idling")

    r_same = two_runners.client.post(
        f"/api/agents/{settled_id}/transfer",
        json={"to_machine": "runner-a"},
    )
    assert r_same.status_code == 409 and "already on machine" in r_same.json()["detail"]

    r_unknown = two_runners.client.post(
        f"/api/agents/{settled_id}/transfer",
        json={"to_machine": "ghost-box"},
    )
    assert r_unknown.status_code == 404

    r_missing_agent = two_runners.client.post(
        "/api/agents/999999/transfer",
        json={"to_machine": "runner-b"},
    )
    assert r_missing_agent.status_code == 404

    db_conn.execute("UPDATE machines SET paused_at = now() WHERE name = 'runner-b'")
    db_conn.commit()
    r_paused = two_runners.client.post(
        f"/api/agents/{settled_id}/transfer",
        json={"to_machine": "runner-b"},
    )
    assert r_paused.status_code == 409 and "paused" in r_paused.json()["detail"]


def test_cutover_cas_conflict_aborts_target_and_keeps_source_intact(
    two_runners: TwoRunnerCluster,
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _seed_native_agent(db_conn, machine="runner-a", status="idling")
    ws_a = paths.workspace_path(two_runners.home_a, agent_id)
    ws_a.mkdir(parents=True)
    (ws_a / "important.txt").write_text("do-not-lose", encoding="utf-8")

    manifest, bundle = pack_workspace(
        home=two_runners.home_a,
        agent_id=agent_id,
        transfer_id="99" * 16,
        source_machine="runner-a",
        killed_shell_sessions=[],
    )
    stage_and_activate_workspace(
        home=two_runners.home_b,
        agent_id=agent_id,
        transfer_id="99" * 16,
        bundle_bytes=bundle,
    )
    assert paths.workspace_path(two_runners.home_b, agent_id).is_dir()

    row = db_conn.execute(
        "SELECT runtime_owner, runtime_generation, last_admission_at, session_index "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row is not None
    snapshot = AgentTransferSnapshot(
        agent_id=agent_id,
        from_machine="runner-a",
        status="idling",
        runtime_owner=row[0],
        runtime_generation=row[1],
        last_admission_at=row[2],
        session_index=int(row[3]),
    )

    db_conn.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (agent_id,))
    db_conn.commit()

    with pytest.raises(CutoverConflictError, match="status changed"):
        commit_cutover_blocking(
            two_runners.client.app.state.db_pool,
            snapshot=snapshot,
            body=TransferAgentRequest(to_machine="runner-b"),
            manifest=manifest,
        )

    abort_target_workspace(home=two_runners.home_b, agent_id=agent_id, transfer_id="99" * 16)
    assert not paths.workspace_path(two_runners.home_b, agent_id).exists()
    assert (ws_a / "important.txt").read_text(encoding="utf-8") == "do-not-lose"
    meta_after = db_conn.execute(
        "SELECT machine, status FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert meta_after == ("runner-a", "running")
