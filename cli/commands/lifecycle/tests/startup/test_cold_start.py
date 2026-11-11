# meiva:note
# The preparation of a start with no live root, run step by step against real resources.
#
# What the preparation is and in which order (`plan_cold_start`) is asserted on values in
# `../start_plans/test_cold_start_plan.py`. These tests run the plan's shell for a runner whose
# data plane is the cluster's (a provider-managed plane in this invocation's configuration, with
# the suite's throwaway Postgres behind the database factory): a real converge step over the
# home, the migration applier, the schema check and the extension registry. The start trace
# is the record of the phases that ran, in order. A gateway's own data-plane steps
# (`DATA_PLANE`, `GATEWAY_SCHEMA`, `POOLER`) start a Postgres and a Redis of their own; they run
# in `../start_storage/test_gateway_cold_start.py`, and here only a gateway whose data-plane step
# fails is shown to end the preparation with that step's code.

from __future__ import annotations

import dataclasses
from pathlib import Path

import psycopg
import pytest

from base.deploy import start_trace
from base.packages.extensions import install_registry
from base.packages.extensions import registry as reg
from base.paths import meiva_home
from cli.commands.converge.spec import ConvergeStep
from cli.commands.converge.steps import ensure_meiva_home_dirs
from cli.commands.lifecycle.converge_host import live_root_steps
from cli.commands.lifecycle.start.cold_start import (
    ColdStart,
    live_schema_behind,
    prepare_cold_start,
    prepare_live_migrations,
    prepare_live_start,
)
from cli.commands.lifecycle.start.cold_start_plan import ColdStep
from cli.commands.lifecycle.start.start_config import read_start_config
from cli.commands.lifecycle.tests.startup.start_harness import StartRun

RUNNER = frozenset({"agent-runner"})
GATEWAY = frozenset({"gateway"})


# meiva:note
# The runner's preparation, with the home-skeleton converge step standing for the whole table
# (each converge step has its own tests, and the full table is `test_cmd_converge.py`'s).
def _cold(
    run: StartRun, *, home: Path | None = None
) -> tuple[ColdStart, list[tuple[str, float, float]]]:
    run.declare(gateway=False, agent_runner=True, remote_plane=True)
    cold = ColdStart(
        command=run.command if home is None else dataclasses.replace(run.command, home=home),
        database_factory=run.database_factory,
        producer=run.producer,
        repo=run.runtime.code_root,
        roles=RUNNER,
        plane=read_start_config(run.command.config).data_plane,
        converge_steps=(ConvergeStep("$MEIVA_HOME dir skeleton", ensure_meiva_home_dirs),),
    )
    return cold, start_trace.trace().spans


def _phases_after(spans: list[tuple[str, float, float]], seen: int) -> list[str]:
    return [name for name, _start, _took in sorted(spans[seen:], key=lambda span: span[1])]


def test_a_runners_preparation_converges_the_home_then_checks_the_schema_and_lands_extensions(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    cold, spans = _cold(run)
    assert cold.plane.is_remote
    seen = len(spans)

    rc = prepare_cold_start(
        cold, run.roster(RUNNER, only=("pty-sessions",)), retained_children=run.children
    )

    assert rc == 0
    assert _phases_after(spans, seen) == [
        "converge host",
        "local services",
        "migrations",
        "schema check",
        "extensions adopt",
        "extensions materialize",
    ]
    for sub in ("logs", "configs", "secrets"):
        assert (run.home / sub).is_dir(), "the converge step laid out the home"
    out = capsys.readouterr().out
    assert "local services: skipped" in out
    assert "applied 0 migration(s)" in out
    assert not (run.home / "pg").exists() and not (run.home / "redis").exists()


# meiva:note
# The first failing step ends the preparation: the converge step cannot lay out a home whose
# `logs` is a symlink, so no migration is attempted and no schema is checked.
def test_a_step_that_fails_stops_the_preparation_where_it_is(
    start_run: StartRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    cold, spans = _cold(run)
    outside = tmp_path / "outside"
    outside.mkdir()
    (run.home / "logs").symlink_to(outside, target_is_directory=True)
    seen = len(spans)

    rc = prepare_cold_start(
        cold, run.roster(RUNNER, only=("pty-sessions",)), retained_children=run.children
    )

    assert rc == 1
    assert _phases_after(spans, seen) == ["converge host"]
    captured = capsys.readouterr()
    assert "converge failed" in captured.err and "symlink" in captured.err
    assert "migration" not in captured.out
    assert not (outside / ".metadata_never_index").exists()


# meiva:note
# An advisory converge step is preparation an optional consumer needs, so its failure must
# not end the preparation: the host has to come up and its later phases still run (a cold
# start returns 1 at the converge step, which would leave the node down -- #6233).
def test_an_advisory_converge_failure_still_prepares_the_host(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    cold, spans = _cold(run)

    def boom(_ctx: object) -> None:
        raise RuntimeError("the npm prefix is root-owned")

    cold = dataclasses.replace(
        cold,
        converge_steps=(
            ConvergeStep("external harness CLIs", boom, on_failure="advisory"),
            ConvergeStep("$MEIVA_HOME dir skeleton", ensure_meiva_home_dirs),
        ),
    )
    seen = len(spans)

    rc = prepare_cold_start(
        cold, run.roster(RUNNER, only=("pty-sessions",)), retained_children=run.children
    )

    assert rc == 0
    phases = _phases_after(spans, seen)
    assert phases[0] == "converge host"
    assert "migrations" in phases, "an advisory failure does not end the preparation"
    captured = capsys.readouterr()
    assert "converge finished with warnings" in captured.err
    assert "npm prefix is root-owned" in captured.err


# meiva:note
# A start that finds the root running prepares the host the same way for the failures a step can
# have: an advisory one is reported and the start goes on, and the step that names a reason not
# to run under a live root does not run.
def test_a_live_root_start_reports_an_advisory_failure_and_leaves_out_the_omitted_step(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    cold, _spans = _cold(run)
    ran: list[str] = []

    def boom(_ctx: object) -> None:
        raise RuntimeError("bat is not installed")

    def record(_ctx: object) -> None:
        ran.append("pooler url")

    table = (
        ConvergeStep("CLI and GNU tools", boom, on_failure="advisory"),
        ConvergeStep("$MEIVA_HOME dir skeleton", ensure_meiva_home_dirs),
        ConvergeStep("pooler url", record, skipped_while_root_runs="the data plane is running"),
    )
    cold = dataclasses.replace(cold, converge_steps=live_root_steps(table), partial_converge=True)

    rc = prepare_live_start(cold, run.roster(RUNNER, only=("pty-sessions",)))

    assert rc == 0
    assert ran == []
    captured = capsys.readouterr()
    assert "converge finished with warnings" in captured.err
    assert "bat is not installed" in captured.err
    assert "✓ $MEIVA_HOME dir skeleton" in captured.out


_SKILL = """---
name: {name}
description: a test skill, use when exercising what a cold start does with extensions
---

# {name}

{body}
"""


def _write_skill(root: Path, name: str, body: str) -> Path:
    tree = root / name
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text(_SKILL.format(name=name, body=body), encoding="utf-8")
    return tree


# meiva:note
# The extension steps act on the invocation's own home and machine name: a skill this machine
# installed before the registry existed is claimed under the machine's name, and a skill another
# machine of the cluster installed lands under this home's `skills/`. The preparation runs in
# the process home so that the install ledger (`installed.json`), which the process home owns, and
# the skills directory it points at are one place.
def test_the_extension_steps_adopt_this_machines_installs_and_land_the_clusters_skills(
    start_run: StartRun, tmp_path: Path, db_conn: psycopg.Connection
) -> None:
    run = start_run
    cold, _spans = _cold(run, home=run.process_home)
    cold.command.machine.set(name="cold-box")
    _write_skill(run.process_home / "skills", "pre-registry", "Installed long ago.")
    install_registry.register(
        install_registry.InstalledPackage(
            name="pre-registry", type="skill", origin="user", enabled=True, trust="unreviewed"
        ),
        home=meiva_home(),
    )
    elsewhere = _write_skill(tmp_path / "elsewhere", "cluster-skill", "From the cluster.")
    with run.database_factory().pool() as pool:
        reg.register_tree(
            pool, root=elsewhere, name="cluster-skill", kind="skill", source="local:other-box"
        )

    rc = prepare_cold_start(
        cold, run.roster(RUNNER, only=("pty-sessions",)), retained_children=run.children
    )

    assert rc == 0
    adopted = reg.get(db_conn, "pre-registry")
    assert adopted is not None and adopted.source == "local:cold-box"
    landed = run.process_home / "skills" / "cluster-skill" / "SKILL.md"
    assert "From the cluster." in landed.read_text(encoding="utf-8")


# meiva:note
# A gateway whose data-plane step reports failure through its return code ends the preparation
# there: nothing after it runs. This home has no start intent, so the step has no cluster record
# to start an instance from and starts nothing.
def test_a_gateways_data_plane_step_that_fails_ends_the_preparation_with_its_code(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    run.declare(gateway=True, agent_runner=False)
    spans = start_trace.trace().spans
    cold = ColdStart(
        command=run.command,
        database_factory=run.database_factory,
        producer=run.producer,
        repo=run.runtime.code_root,
        roles=GATEWAY,
        plane=read_start_config(run.command.config).data_plane,
        converge_steps=(ConvergeStep("$MEIVA_HOME dir skeleton", ensure_meiva_home_dirs),),
    )
    seen = len(spans)

    rc = prepare_cold_start(cold, (), retained_children=run.children)

    assert rc == 1
    assert _phases_after(spans, seen) == ["converge host", "data plane (pg + redis)"]
    assert not (run.home / "pg").exists() and not (run.home / "redis").exists()
    assert "migration" not in capsys.readouterr().out


def _forget_newest_migration(db_conn: psycopg.Connection) -> str:
    newest = db_conn.execute(
        "SELECT name FROM schema_migrations ORDER BY name DESC LIMIT 1"
    ).fetchone()
    assert newest is not None
    db_conn.execute("DELETE FROM schema_migrations WHERE name = %s", newest)
    db_conn.commit()
    return str(newest[0])


# meiva:note
# 2026-11-11: a gateway whose root survived the update (`--keep-shell-sessions`) found a live root
# and refused the migrations the new code carries, so the whole production update stopped. With no
# database-using service running (the stop took them down), the live start applies the pending
# migrations through the cold path's own step. The pooler completion that follows has its own tests.
def test_a_live_root_start_applies_the_pending_migrations_when_no_database_service_runs(
    start_run: StartRun,
    db_conn: psycopg.Connection,
    db_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = start_run
    cold, _spans = _cold(run)
    forgotten = _forget_newest_migration(db_conn)
    try:
        assert live_schema_behind(db_url), "the database lacks a migration the checkout carries"

        rc = prepare_live_migrations(
            cold,
            run.roster(RUNNER, only=("pty-sessions",)),
            steps=(ColdStep.MIGRATIONS,),
            running=(),
            retained_children=run.children,
        )

        assert rc == 0
        assert f"applied 1 migration(s): ['{forgotten}']" in capsys.readouterr().out
        assert not live_schema_behind(db_url)
    finally:
        db_conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING", (forgotten,)
        )
        db_conn.commit()


def test_a_live_root_start_applies_nothing_while_a_database_service_runs(
    start_run: StartRun,
    db_conn: psycopg.Connection,
    db_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = start_run
    cold, _spans = _cold(run)
    forgotten = _forget_newest_migration(db_conn)
    try:
        rc = prepare_live_migrations(
            cold,
            run.roster(RUNNER, only=("pty-sessions",)),
            steps=(ColdStep.MIGRATIONS,),
            running=("agent-host",),
            retained_children=run.children,
        )

        assert rc == 1
        assert "still hosts services that use it: agent-host" in capsys.readouterr().err
        assert live_schema_behind(db_url), "nothing was applied"
    finally:
        db_conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING", (forgotten,)
        )
        db_conn.commit()
