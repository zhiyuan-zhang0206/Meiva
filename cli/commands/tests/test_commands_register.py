from __future__ import annotations

import re
from dataclasses import replace
from typing import cast

import pytest

import cli.commands.probe as _probe_commands
import services.supervision.roster.service_spec as _service_spec
from base.config import get_field
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.paths import meiva_home, run_dir
from cli.commands._repo import register_machine_or_die
from cli.commands._setup import SetupValues
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.supervision.roster.composition import build_services


def _spec_by_service(service: str) -> _service_spec.ServiceSpec:
    for spec in build_services(home=meiva_home()):
        if spec.session == service:
            return spec
    raise AssertionError(f"no ServiceSpec service={service!r}")


def test_gateway_spec_uses_http_probe_not_pidfile() -> None:
    spec = _spec_by_service("gateway")
    assert spec.curl_url is not None, "gateway must use curl probe (uvicorn reload)"
    assert spec.curl_url.startswith("http://"), f"curl_url shape wrong: {spec.curl_url!r}"


def test_probe_gateway_takes_the_identity_path() -> None:

    spec = _spec_by_service("gateway")
    from base.daemon.health import DaemonProbe

    spec = replace(spec, identity_probe=lambda: DaemonProbe.up("root-owned gateway"))
    probe = _probe_commands.probe_service(spec)
    assert probe.alive is True
    assert probe.label == "identity"


def test_probe_gateway_reports_which_fact_failed(monkeypatch: pytest.MonkeyPatch) -> None:

    spec = _spec_by_service("gateway")
    from base.daemon.health import DaemonProbe

    spec = replace(
        spec,
        identity_probe=lambda: DaemonProbe.port_taken(
            "identity mismatch: home='/home/meiva/.meiva'"
        ),
    )
    probe = _probe_commands.probe_service(spec)
    assert probe.alive is False
    assert "/home/meiva/.meiva" in probe.detail


def test_probe_survives_an_identity_probe_that_raises() -> None:
    import dataclasses

    def _boom() -> object:
        raise RuntimeError("no socket for you")

    spec = dataclasses.replace(_spec_by_service("gateway"), identity_probe=_boom)
    probe = _probe_commands.probe_service(spec)
    assert probe.alive is None
    assert probe.label == "unavailable"
    assert "RuntimeError" in probe.detail
    assert "no socket for you" in probe.detail


def test_service_without_identity_probe_cannot_claim_readiness() -> None:
    spec = replace(_spec_by_service("gateway"), identity_probe=None)
    result = _probe_commands.probe_service(spec)
    assert result.alive is None
    assert result.label == "unavailable"


# meiva:note
# The dial URL a unit registered, read back from the machine registry's unit rows.
def _registered_url(db: Database, machine_name: str) -> str | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT url FROM machine_units WHERE machine_name = %s", (machine_name,))
        rows = cur.fetchall()
    assert len(rows) == 1
    return rows[0][0]


def test_register_gateway_advertises_without_gateway_url(
    database: Database,
    *,
    command_runtime: CommandRuntime,
) -> None:
    command_runtime.config.set_field("gateway_url", "")
    command_runtime.config.set_field("gateway_port", 8000)
    command_runtime.machine.set(host="10.0.0.2")
    db = database

    rc = register_machine_or_die(
        db,
        cast(SetupValues, {"machine_name": "control"}),
        frozenset({"gateway"}),
        command=command_runtime,
    )
    assert rc == 0
    assert _registered_url(db, command_runtime.machine.name()) == "http://10.0.0.2:8000"


def test_register_gateway_only_advertises_reachable_host(
    database: Database,
    *,
    command_runtime: CommandRuntime,
) -> None:
    command_runtime.config.set_field("gateway_url", "https://meiva.example:8000")
    command_runtime.machine.set(host="10.0.0.2")
    db = database

    rc = register_machine_or_die(
        db,
        cast(SetupValues, {"machine_name": "control"}),
        frozenset({"gateway"}),
        command=command_runtime,
    )
    assert rc == 0
    assert _registered_url(db, command_runtime.machine.name()) == "http://10.0.0.2:8000"


def test_register_agent_runner_advertises_ops_url(
    database: Database,
    *,
    command_runtime: CommandRuntime,
) -> None:
    command_runtime.machine.set(host="10.0.0.2")
    ops_port = (
        ServiceEndpoints.from_fields(get_field, runtime=run_dir(meiva_home())).of("ops").health_port
    )
    db = database

    rc = register_machine_or_die(
        db,
        cast(SetupValues, {"machine_name": "runner-a"}),
        frozenset({"agent-runner"}),
        command=command_runtime,
    )
    assert rc == 0
    assert _registered_url(db, command_runtime.machine.name()) == f"http://10.0.0.2:{ops_port}"


def test_register_agent_runner_loopback_host_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
    database: Database,
    *,
    command_runtime: CommandRuntime,
) -> None:
    command_runtime.machine.set(host="127.0.0.1")
    db = database

    rc = register_machine_or_die(
        db,
        cast(SetupValues, {"machine_name": "runner-a"}),
        frozenset({"agent-runner"}),
        command=command_runtime,
    )
    assert rc == 1
    assert "would register a loopback dial URL" in capsys.readouterr().err
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM machine_units WHERE machine_name = %s",
            (command_runtime.machine.name(),),
        )
        row = cur.fetchone()
    assert row == (0,)


def _named_commands(text: str) -> set[str]:
    return set(re.findall(r"`(meiva [^`]+)`", text))


def test_register_schema_behind_hint_names_working_commands(
    capsys: pytest.CaptureFixture[str],
    database: Database,
    *,
    command_runtime: CommandRuntime,
) -> None:
    command_runtime.machine.set(host="10.0.0.2")
    db = database
    with db.write_transaction() as conn:
        conn.execute("ALTER TABLE machine_units RENAME TO machine_units_gone")
        conn.commit()
    try:
        rc = register_machine_or_die(
            db,
            cast(SetupValues, {"machine_name": "gw"}),
            frozenset({"gateway"}),
            command=command_runtime,
        )
    finally:
        with db.write_transaction() as conn:
            conn.execute("ALTER TABLE machine_units_gone RENAME TO machine_units")
            conn.commit()

    assert rc == 1
    err = capsys.readouterr().err
    assert "`meiva stop` then `meiva start` on the gateway" in err
    assert _named_commands(err) == {"meiva stop", "meiva start"}


def test_code_behind_schema_hint_names_working_commands(
    capsys: pytest.CaptureFixture[str], db_url: str, database: Database
) -> None:
    import cli.commands._repo as _repo_commands

    db = database
    future = "99991231T235959_a_migration_this_checkout_lacks"
    with db.write_transaction() as conn:
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (future,))
        conn.commit()
    try:
        assert _repo_commands.assert_schema_current_or_die(db_url) == 1
    finally:
        with db.write_transaction() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE name = %s", (future,))
            conn.commit()
    err = capsys.readouterr().err
    assert "gateway's revision" in err
    assert _named_commands(err) == {"meiva start"}
