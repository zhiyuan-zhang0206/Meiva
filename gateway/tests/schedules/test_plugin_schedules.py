from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from base.daemon.schedules.plugin_schedules import (
    PluginScheduleSyncSummary,
)
from base.daemon.schedules.plugin_schedules import (
    reconcile_plugin_schedules as _reconcile_plugin_schedules,
)
from base.packages.plugins.enable_config import write_local
from gateway.tests.schedules.api_support import schedule_client

_NOW = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)


def reconcile_plugin_schedules(
    conn: psycopg.Connection,
    *,
    home: Path,
    cluster_timezone: str = "UTC",
    now: datetime = _NOW,
) -> PluginScheduleSyncSummary:
    return _reconcile_plugin_schedules(
        conn,
        home=home,
        cluster_timezone=cluster_timezone,
        now=now,
    )


def _make_plugin(
    home: Path,
    name: str,
    *,
    schedules_code: str,
    enabled: bool = True,
) -> Path:
    pdir = home / "plugins" / name
    pdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "apiVersion": 2,
        "name": name,
        "version": "1.0.0",
        "description": f"Test plugin {name}",
        "engines": {"meiva": ">=2026.10.1"},
        "contributions": {"schedules": {"face": "schedules.py"}},
    }
    (pdir / "meiva-plugin.json").write_text(json.dumps(manifest))
    (pdir / "plugin.py").write_text("# plugin\n")
    (pdir / "schedules.py").write_text(schedules_code)

    write_local({"plugins": {name: {"enabled": enabled}}}, home=home)
    return pdir


def test_reconcile_creates_plugin_schedules(db_conn: psycopg.Connection, unit_home: Path) -> None:
    code_v1 = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            trigger_kind='every', interval_seconds=60,\n"
        "            name='my_plug:task1',\n"
        "            machine='gateway',\n"
        "            description='Task 1 V1',\n"
        "            script='print(\"v1\")\\n',\n"
        "            command='python schedule.py',\n"
        "            secret_env_vars=('SEC_A',),\n"
        "            default_enabled=True,\n"
        "        ),\n"
        "    )\n"
    )
    _make_plugin(unit_home, "my_plug", schedules_code=code_v1)

    summary = reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()
    assert summary.created == ("my_plug:task1",)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, owner_plugin, machine, script, secret_env_vars, desired_revision "
            "FROM schedules WHERE name = 'my_plug:task1'"
        )
        row = cur.fetchone()
        assert row is not None
        sched_id, owner, machine, script, secrets, rev = row
        assert owner == "my_plug"
        assert machine == "gateway"
        assert script == 'print("v1")\n'
        assert secrets == ["SEC_A"]
        assert rev == 0

        cur.execute(
            "SELECT script, note FROM schedule_versions WHERE schedule_id = %s", (sched_id,)
        )
        versions = cur.fetchall()
        assert len(versions) == 1
        assert versions[0][1] == "plugin-initial"


def test_reconcile_updates_and_moves_machine(db_conn: psycopg.Connection, unit_home: Path) -> None:
    code_v1 = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            trigger_kind='every', interval_seconds=60,\n"
        "            name='my_plug:task1',\n"
        "            machine='gateway',\n"
        "            description='Task 1 V1',\n"
        "            script='print(\"v1\")\\n',\n"
        "            command='python schedule.py',\n"
        "            default_enabled=True,\n"
        "        ),\n"
        "    )\n"
    )
    pdir = _make_plugin(unit_home, "my_plug", schedules_code=code_v1)
    reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM schedules WHERE name = 'my_plug:task1'")
        row = cur.fetchone()
        assert row is not None
        sched_id = row[0]

    code_v2 = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            trigger_kind='every', interval_seconds=60,\n"
        "            name='my_plug:task1',\n"
        "            machine='worker-box',\n"
        "            description='Task 1 V2',\n"
        "            script='print(\"v2\")\\n',\n"
        "            command='python schedule.py',\n"
        "            secret_env_vars=('SEC_A', 'SEC_B'),\n"
        "            default_enabled=True,\n"
        "        ),\n"
        "    )\n"
    )
    (pdir / "schedules.py").write_text(code_v2)

    summary2 = reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()
    assert summary2.updated == ("my_plug:task1",)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT machine, script, secret_env_vars, desired_revision FROM schedules WHERE id = %s",
            (sched_id,),
        )
        row2 = cur.fetchone()
        assert row2 is not None
        assert row2[0] == "worker-box"
        assert row2[1] == 'print("v2")\n'
        assert row2[2] == ["SEC_A", "SEC_B"]
        assert row2[3] == 1

        cur.execute(
            "SELECT machine FROM schedule_sync_requests WHERE schedule_id = %s ORDER BY machine",
            (sched_id,),
        )
        sync_machines = [r[0] for r in cur.fetchall()]
        assert "gateway" in sync_machines
        assert "worker-box" in sync_machines


def test_reconcile_preserves_operator_enabled_state(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            trigger_kind='every', interval_seconds=60,\n"
        "            name='my_plug:task',\n"
        "            machine='gateway',\n"
        "            description='Task',\n"
        "            script='print(1)\\n',\n"
        "            command='python schedule.py',\n"
        "            default_enabled=True,\n"
        "        ),\n"
        "    )\n"
    )
    _make_plugin(unit_home, "my_plug", schedules_code=code)
    reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE name = 'my_plug:task'")
    db_conn.commit()

    summary = reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()
    assert summary.created == ()

    with db_conn.cursor() as cur:
        cur.execute("SELECT enabled FROM schedules WHERE name = 'my_plug:task'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is False


def test_reconcile_updates_trigger_in_place(db_conn: psycopg.Connection, unit_home: Path) -> None:
    initial_code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (ScheduleSpec(name='my_plug:task', machine='gateway', "
        "description='Task', script='print(1)\\n', trigger_kind='every', interval_seconds=60),)\n"
    )
    pdir = _make_plugin(unit_home, "my_plug", schedules_code=initial_code)
    reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()
    old = db_conn.execute(
        "SELECT id, enabled FROM schedules WHERE name = 'my_plug:task'"
    ).fetchone()
    assert old is not None

    declarative_code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (ScheduleSpec(name='my_plug:task', machine='gateway', "
        "description='Task', script='print(2)\\n', trigger_kind='every', "
        "interval_seconds=300, timeout_seconds=45),)\n"
    )
    (pdir / "schedules.py").write_text(declarative_code)
    now = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)

    summary = reconcile_plugin_schedules(
        db_conn,
        home=unit_home,
        cluster_timezone="Asia/Shanghai",
        now=now,
    )
    db_conn.commit()

    assert summary.updated == ("my_plug:task",)
    stored = db_conn.execute(
        "SELECT id, enabled, status, trigger_kind, interval_seconds, timezone, timeout_seconds, "
        "next_launch_at FROM schedules WHERE name = 'my_plug:task'"
    ).fetchone()
    assert stored == (
        old[0],
        old[1],
        "scheduled",
        "every",
        300,
        "Asia/Shanghai",
        45,
        now + timedelta(seconds=300),
    )
    assert db_conn.execute(
        "SELECT machine FROM schedule_sync_requests WHERE schedule_id = %s", (old[0],)
    ).fetchall() == [("gateway",)]


def test_reconcile_removes_orphans_on_disable(db_conn: psycopg.Connection, unit_home: Path) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            trigger_kind='every', interval_seconds=60,\n"
        "            name='my_plug:temp',\n"
        "            machine='gateway',\n"
        "            description='Temp',\n"
        "            script='print(1)\\n',\n"
        "            command='python schedule.py',\n"
        "        ),\n"
        "    )\n"
    )
    _make_plugin(unit_home, "my_plug", schedules_code=code, enabled=True)
    reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()

    write_local({"plugins": {"my_plug": {"enabled": False}}}, home=unit_home)

    summary = reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()
    assert summary.removed == ("my_plug:temp",)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM schedules WHERE name = 'my_plug:temp'")
        assert cur.fetchone() is None


def test_reconcile_failed_plugin_preserves_rows(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            trigger_kind='every', interval_seconds=60,\n"
        "            name='my_plug:safe',\n"
        "            machine='gateway',\n"
        "            description='Safe',\n"
        "            script='print(1)\\n',\n"
        "            command='python schedule.py',\n"
        "        ),\n"
        "    )\n"
    )
    pdir = _make_plugin(unit_home, "my_plug", schedules_code=code, enabled=True)
    reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()

    (pdir / "schedules.py").write_text("raise RuntimeError('broken!')\n")

    summary = reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()
    assert summary.removed == ()

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM schedules WHERE name = 'my_plug:safe'")
        assert cur.fetchone() is not None


def test_api_guards_prevent_manual_delete_and_code_update(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    code = (
        "from base.packages.plugins.surface.schedules import ScheduleSpec\n\n"
        "def contribute(config):\n"
        "    return (\n"
        "        ScheduleSpec(\n"
        "            trigger_kind='every', interval_seconds=60,\n"
        "            name='guard_plug:task',\n"
        "            machine='gateway',\n"
        "            description='Guarded Task',\n"
        "            script='print(1)\\n',\n"
        "            command='python schedule.py',\n"
        "            default_enabled=True,\n"
        "        ),\n"
        "    )\n"
    )
    _make_plugin(unit_home, "guard_plug", schedules_code=code, enabled=True)
    reconcile_plugin_schedules(db_conn, home=unit_home)
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM schedules WHERE name = 'guard_plug:task'")
        row = cur.fetchone()
        assert row is not None
        sched_id = row[0]

    with schedule_client() as client:
        del_resp = client.delete(f"/api/schedules/{sched_id}")
        assert del_resp.status_code == 409
        assert "owned by plugin 'guard_plug'" in del_resp.json()["detail"]

        put_resp = client.put(f"/api/schedules/{sched_id}", json={"script": "print(2)\n"})
        assert put_resp.status_code == 409
        assert "only enabled may be toggled directly" in put_resp.json()["detail"]

        toggle_resp = client.put(f"/api/schedules/{sched_id}", json={"enabled": False})
        assert toggle_resp.status_code == 200
        assert toggle_resp.json()["enabled"] is False
