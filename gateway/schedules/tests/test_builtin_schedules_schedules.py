from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base.daemon.schedules.builtin_schedules import (
    ProvisionResult,
    provision_builtin_schedules,
)
from gateway.schedules.session_control import run_plugin_schedules_reconciler

_ENTRY: dict[str, object] = {
    "name": "prod-a",
    "class": "product",
    "default_enabled": True,
    "script": "a.py",
    "command": "python a.py",
    "description": "A",
}


def _manifest(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    for e in entries:
        script = e["script"]
        assert isinstance(script, str)
        (tmp_path / script).write_text("print('ok')\n")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": 1, "builtin_schedules": entries})
    )
    return tmp_path / "manifest.json"


def _names(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM schedules")
        return {r[0] for r in cur.fetchall()}


def _row(conn: psycopg.Connection, name: str) -> tuple[object, ...]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, enabled, script, command FROM schedules WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def _versions(conn: psycopg.Connection, name: str) -> list[tuple[str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.note FROM schedule_versions v JOIN schedules s ON s.id = v.schedule_id "
            "WHERE s.name = %s ORDER BY v.id",
            (name,),
        )
        return cur.fetchall()


class TestProvision:
    def test_creates_missing_with_manifest_defaults(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "prod-a",
                    "class": "product",
                    "default_enabled": True,
                    "script": "a.py",
                    "command": "python a.py",
                    "description": "A",
                },
                {
                    "name": "op-b",
                    "class": "operator",
                    "default_enabled": False,
                    "script": "b.py",
                    "command": "python b.py",
                    "description": "B",
                },
            ],
        )
        created = provision_builtin_schedules(db_conn, machine="gateway", path=path)
        db_conn.commit()
        assert created.created == ["prod-a", "op-b"] and created.resynced == []
        assert _row(db_conn, "prod-a")[1] is True
        assert _row(db_conn, "op-b")[1] is False
        assert _row(db_conn, "op-b")[2] == "print('ok')\n"

    def test_idempotent_never_touches_existing(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "prod-a",
                    "class": "product",
                    "default_enabled": True,
                    "script": "a.py",
                    "command": "python a.py",
                    "description": "A",
                },
            ],
        )
        assert provision_builtin_schedules(db_conn, machine="gateway", path=path).created == [
            "prod-a"
        ]
        db_conn.commit()
        assert (
            provision_builtin_schedules(db_conn, machine="gateway", path=path) == ProvisionResult()
        )
        db_conn.commit()
        assert _row(db_conn, "prod-a") == ("prod-a", True, "print('ok')\n", "python a.py")
        assert _versions(db_conn, "prod-a") == [("initial",)]

    def test_a_drifted_builtin_follows_the_template_and_keeps_its_operator_state(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        path = _manifest(tmp_path, [_ENTRY])
        provision_builtin_schedules(db_conn, machine="gateway", path=path)
        db_conn.commit()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE schedules SET enabled = false, description = 'tuned', "
                "script = 'catch_up([])\n', command = 'python old.py' WHERE name = 'prod-a'"
            )
        db_conn.commit()

        result = provision_builtin_schedules(db_conn, machine="gateway", path=path)
        db_conn.commit()

        assert result == ProvisionResult(created=[], resynced=["prod-a"])
        assert _row(db_conn, "prod-a") == ("prod-a", False, "print('ok')\n", "python a.py")
        with db_conn.cursor() as cur:
            cur.execute("SELECT description FROM schedules WHERE name = 'prod-a'")
            assert cur.fetchone() == ("tuned",)
            cur.execute(
                "SELECT count(*) FROM schedule_sync_requests q JOIN schedules s "
                "ON s.id = q.schedule_id WHERE s.name = 'prod-a'"
            )
            assert cur.fetchone() == (1,)
        versions = _versions(db_conn, "prod-a")
        assert versions[0] == ("initial",) and versions[1][0].startswith("builtin-resync ")

        assert (
            provision_builtin_schedules(db_conn, machine="gateway", path=path) == ProvisionResult()
        )
        db_conn.commit()
        assert len(_versions(db_conn, "prod-a")) == 2

    def test_a_schedule_outside_the_manifest_is_never_touched(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        path = _manifest(tmp_path, [_ENTRY])
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedules (name, description, script, command, enabled, machine) "
                "VALUES ('agent-made', 'x', 'print(1)\n', 'python x.py', true, 'gateway')"
            )
        db_conn.commit()
        assert provision_builtin_schedules(db_conn, machine="gateway", path=path).created == [
            "prod-a"
        ]
        db_conn.commit()
        assert _row(db_conn, "agent-made") == ("agent-made", True, "print(1)\n", "python x.py")

    def test_manifest_driven_restore(self, db_conn: psycopg.Connection, tmp_path: Path) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "prod-a",
                    "class": "product",
                    "default_enabled": True,
                    "script": "a.py",
                    "command": "python a.py",
                    "description": "A",
                },
            ],
        )
        provision_builtin_schedules(db_conn, machine="gateway", path=path)
        db_conn.commit()
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM schedules WHERE name = 'prod-a'")
        db_conn.commit()
        assert provision_builtin_schedules(db_conn, machine="gateway", path=path).created == [
            "prod-a"
        ]
        db_conn.commit()

    def test_per_machine_placement_provisions_for_each_runner(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "per-machine-job",
                    "class": "product",
                    "default_enabled": True,
                    "script": "a.py",
                    "command": "python a.py",
                    "description": "Per machine job",
                    "placement": "per_machine",
                },
            ],
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO machines (name, role) VALUES "
                "('runner-a', '{agent-runner}'), ('runner-b', '{agent-runner}') "
                "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role"
            )
        db_conn.commit()

        result = provision_builtin_schedules(db_conn, machine="gateway", path=path)
        db_conn.commit()

        assert set(result.created) == {
            "per-machine-job:runner-a",
            "per-machine-job:runner-b",
        }
        assert _row(db_conn, "per-machine-job:runner-a")[1] is True
        assert _row(db_conn, "per-machine-job:runner-b")[1] is True

        again = provision_builtin_schedules(db_conn, machine="gateway", path=path)
        db_conn.commit()
        assert again == ProvisionResult()

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE schedules SET script = 'drifted\n' WHERE name = 'per-machine-job:runner-a'"
            )
        db_conn.commit()
        resynced = provision_builtin_schedules(db_conn, machine="gateway", path=path)
        db_conn.commit()
        assert resynced == ProvisionResult(created=[], resynced=["per-machine-job:runner-a"])

        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO machines (name, role) VALUES ('runner-c', '{agent-runner}') "
                "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role"
            )
        db_conn.commit()
        joined = provision_builtin_schedules(db_conn, machine="gateway", path=path)
        db_conn.commit()
        assert joined == ProvisionResult(created=["per-machine-job:runner-c"], resynced=[])

    async def test_reconciler_provisions_harness_cli_update_when_runner_added(
        self, db_conn: psycopg.Connection, db_url: str, tmp_path: Path
    ) -> None:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM machines")
            cur.execute("DELETE FROM schedules")
        db_conn.commit()

        pool: ConnectionPool = ConnectionPool(db_url, min_size=1, max_size=2, open=True)
        try:
            task = asyncio.create_task(
                run_plugin_schedules_reconciler(
                    pool,
                    tmp_path,
                    interval_seconds=0.02,
                    resolve_machine=lambda: "gateway",
                    provision_builtins=True,
                )
            )
            await asyncio.sleep(0.05)

            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO machines (name, role) VALUES ('runner-tick', '{agent-runner}') "
                    "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role"
                )
            db_conn.commit()

            for _ in range(50):
                if "harness-cli-update:runner-tick" in _names(db_conn):
                    break
                await asyncio.sleep(0.05)

            assert "harness-cli-update:runner-tick" in _names(db_conn)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            pool.close()
