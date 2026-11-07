# meiva:note
# `attached` on a terminate response: the harness registration the agent's home machine holds.
#
# The row belongs to the bridge, not to this tree, so these tests write it through
# `base.lm.harness.registration.RegistrationStore` — the writer's own format — and then check
# the response reports exactly those facts. The absent and undecodable cases matter as much as
# the present one: the hint is advisory, so neither may change what the terminate did.

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.lm.harness.registration import HarnessRegistration, RegistrationStore
from base.paths import harness_registrations_dir
from ops import lifecycle
from ops.lifecycle import termination
from ops.lifecycle.tests.loopback.agent_host import ServedAgentHost
from ops.rpc_schemas import TerminateAgentRequest

_CONTROLLER_PID = 4242
_EXPIRES_AT = "2026-11-08T01:00:00+00:00"


def _write(home: Path, agent_id: int) -> None:
    RegistrationStore(harness_registrations_dir(home=home)).write(
        HarnessRegistration(
            agent_id=agent_id,
            harness="claude_code",
            external_session_id="session-1",
            machine="test-machine",
            controller_pid=_CONTROLLER_PID,
            handle_sha256="0" * 64,
            created_at="2026-11-08T00:00:00+00:00",
            expires_at=_EXPIRES_AT,
            status="attached",
        )
    )


def _terminated_agent(db_conn: psycopg.Connection) -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id,status,machine,termination_source) "
        "VALUES (%s,'terminated','test-machine','exit')",
        (agent_id,),
    )
    db_conn.commit()
    return agent_id


class TestHarnessAttachment:
    def test_a_written_row_is_reported(self, tmp_path: Path) -> None:
        _write(tmp_path, 7)

        attachment = termination.harness_attachment(7, home=tmp_path)

        assert attachment is not None
        assert attachment.agent_id == 7
        assert attachment.controller_pid == _CONTROLLER_PID
        assert attachment.expires_at == _EXPIRES_AT

    # meiva:note
    # An expired claim is still an answer: the row reports its own past instant instead of
    # collapsing into the "nobody is attached" answer, which is the distinction a caller needs
    # before deciding whether to kill an agent somebody may still be driving.
    def test_a_row_whose_claim_lapsed_is_still_reported(self, tmp_path: Path) -> None:
        RegistrationStore(harness_registrations_dir(home=tmp_path)).write(
            HarnessRegistration(
                agent_id=7,
                harness="claude_code",
                external_session_id="session-1",
                machine="test-machine",
                controller_pid=_CONTROLLER_PID,
                handle_sha256="0" * 64,
                created_at="2026-11-01T00:00:00+00:00",
                expires_at="2026-11-01T01:00:00+00:00",
                status="attached",
            )
        )

        attachment = termination.harness_attachment(7, home=tmp_path)

        assert attachment is not None
        assert attachment.expires_at == "2026-11-01T01:00:00+00:00"

    def test_no_row_is_none(self, tmp_path: Path) -> None:
        assert termination.harness_attachment(7, home=tmp_path) is None

    def test_a_row_that_is_not_json_is_none(self, tmp_path: Path) -> None:
        path = harness_registrations_dir(home=tmp_path) / "7.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        assert termination.harness_attachment(7, home=tmp_path) is None

    def test_a_row_missing_a_required_field_is_none(self, tmp_path: Path) -> None:
        path = harness_registrations_dir(home=tmp_path) / "7.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"agent_id": 7}', encoding="utf-8")

        assert termination.harness_attachment(7, home=tmp_path) is None


class TestTerminateReportsTheRow:
    async def test_the_response_carries_the_controller_and_its_expiry(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        agent_host: ServedAgentHost,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        agent_id = _terminated_agent(db_conn)
        _write(agent_host.host.home, agent_id)

        resp = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            agent_id,
            TerminateAgentRequest(),
            db_pool,
            host=agent_host.host,
        )

        assert resp.status == "already_terminated"
        assert resp.attached is not None
        assert resp.attached.agent_id == agent_id
        assert resp.attached.controller_pid == _CONTROLLER_PID
        assert resp.attached.expires_at == _EXPIRES_AT

    # meiva:note
    # The positive control for the test above: same call, no row on disk — the response is still
    # `already_terminated` and `attached` is null. Without this, a wire field that never gets
    # filled and a field that is filled correctly would look the same.
    async def test_without_a_row_the_terminate_is_unchanged(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        agent_host: ServedAgentHost,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        agent_id = _terminated_agent(db_conn)

        resp = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            agent_id,
            TerminateAgentRequest(),
            db_pool,
            host=agent_host.host,
        )

        assert resp.status == "already_terminated"
        assert resp.attached is None
