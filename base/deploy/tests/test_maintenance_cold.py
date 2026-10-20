from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.maintenance import cohort, pause_owner
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus


def _cold_agent_with_a_restart_and_a_claimed_chat(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
    *,
    restart_status: str,
) -> tuple[int, int]:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    chat = insert_inbound_message(
        db_conn, agent, "unfinished action", "user", bus=event_bus, database=database
    )
    restart = insert_inbound_message(
        db_conn, agent, "", "system:restart", kind="restart", bus=event_bus, database=database
    )
    db_conn.execute("UPDATE inbound_messages SET status='claimed' WHERE id=%s", (chat,))
    db_conn.execute(
        "UPDATE inbound_messages SET status=%s,claimed_at=clock_timestamp() WHERE id=%s",
        (restart_status, restart),
    )
    db_conn.commit()
    return agent, chat


def test_cold_executed_restart_and_claimed_chat_are_preserved(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
) -> None:
    agent, chat = _cold_agent_with_a_restart_and_a_claimed_chat(
        db_conn, database, event_bus, machine_identity, restart_status="done"
    )
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    pause_owner.begin_maintenance("cold", WHEN)
    held = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="cold",
        acquired_at=WHEN,
        host_absent=True,
    )
    assert held.parked == (agent,) and held.commands == {} and held.drained == ()
    cohort.verify_drained(db_conn, held)
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (chat,)
    ).fetchone() == ("claimed",)


def test_cold_agent_with_an_open_restart_refuses_preparation(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
) -> None:
    _cold_agent_with_a_restart_and_a_claimed_chat(
        db_conn, database, event_bus, machine_identity, restart_status="claimed"
    )
    pause_owner.begin_maintenance("cold", WHEN)
    with pytest.raises(cohort.LifecycleCollisionError):
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=None,
            holder="cold",
            acquired_at=WHEN,
            host_absent=True,
        )


def test_expired_host_lease_does_not_prove_normal_shutdown(
    db_conn: psycopg.Connection[Any],
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_owner=%s,runtime_generation=%s,"
        "lease_expires_at=%s WHERE id=%s",
        (uuid4(), uuid4(), WHEN - timedelta(days=1), agent),
    )
    db_conn.commit()
    pause_owner.begin_maintenance("cold", WHEN)
    with pytest.raises(RuntimeError, match="live original native"):
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=None,
            holder="cold",
            acquired_at=WHEN,
            host_absent=True,
        )


def test_consumer_scan_skips_a_process_whose_cmdline_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.deploy.maintenance import cold

    class _ReadFails:
        pid = 424242

        @property
        def info(self) -> dict[str, object]:
            raise SystemError("psutil: cmdline read failed mid-iteration")

        def cmdline(self) -> list[str]:
            raise SystemError("psutil: cmdline read failed mid-iteration")

    class _Consumer:
        pid = 5005

        def cmdline(self) -> list[str]:
            return ["python", "-m", "services.agent_runner.agent_host.daemon"]

        def environ(self) -> dict[str, str]:
            return {}

    procs: list[object] = [_ReadFails(), _Consumer()]

    def process_iter(*args: object, **kwargs: object) -> Iterator[object]:
        del args, kwargs
        return iter(procs)

    monkeypatch.setattr(cold.psutil, "process_iter", process_iter)
    with pytest.raises(RuntimeError, match="native consumer still exists during cold prepare"):
        cold.require_no_consumer_process()


def test_consumer_scan_reports_a_process_whose_cmdline_it_cannot_read(
    loguru_records: list[dict[str, Any]],
) -> None:
    from base.deploy.maintenance import cold

    class _ReadFails:
        pid = 424242

        def cmdline(self) -> list[str]:
            raise SystemError("psutil: cmdline read failed mid-iteration")

    cold.require_no_consumer_process(processes=[_ReadFails()])

    assert any(
        "could not read the argv of 1 process" in record["message"] for record in loguru_records
    ), "an argv the scan could not read was skipped without a word; it may be a consumer"
