# meiva:note
# Real inputs for the cluster health probe's tests.
#
# A probe round is run against what it would observe in production, at test scale: a scripted
# gateway on a loopback port, the throwaway Postgres and Redis the suite provisions, a home
# under `tmp_path` whose root manifest says what the last start launched, and the capture
# pipeline that records the events the round emits. Nothing the probe calls is replaced; a
# fault is made the way it happens (a port nobody listens on, a row that is missing).

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_settings
from base.host.net.tests.local_http import closed_port_url
from base.paths import root_manifests_path
from base.telemetry.tests.capture.event_capture import EmittedEvents, FollowedAttributes
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.cluster._provider_guard import ProviderGuardConfig
from cli.commands.cluster.health_readings import HealthOptions
from cli.commands.cluster.plans.health_verdicts import HealthRules
from cli.commands.cluster.tests.gateway_server import Reply, ScriptedGateway, serving
from cli.commands.data_plane.config import data_plane_config_from_boot
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.supervision.meiva_root_glue import manifests
from services.supervision.roster.composition import build_services

__all__ = [
    "emitted_events",
    "entry_command",
    "gateway",
    "git_checkout",
    "healthy_options",
    "launch_record",
    "live_agent",
    "operator_database",
    "probe_command",
    "provider_off",
    "ran",
    "signals",
    "unreachable_database",
]


@pytest.fixture
def gateway() -> Iterator[ScriptedGateway]:
    """A gateway whose `/api/health` answers 200; a test scripts other answers with `serving`."""
    with serving({("GET", "/api/health"): Reply(body={"status": "ok"})}) as scripted:
        yield scripted


# meiva:note
# Both provider checks off, so a round never dials the provider or reads the agent table for
# them; the provider tests turn them on one at a time.
def provider_off() -> ProviderGuardConfig:
    return ProviderGuardConfig(
        balance_enabled=False,
        deepseek_api_key=None,
        balance_url="http://127.0.0.1:1/balance",
        balance_timeout_s=2.0,
        balance_min_cny=500.0,
        blocked_agents_enabled=False,
        blocked_agents_window_hours=24.0,
        blocked_agents_min=3,
    )


# meiva:note
# A round that holds a healthy cluster to its rules: the gateway of the test, one live agent
# required, no prod checkout to guard, WAL-G off, the volume of `tmp_path` with a non-tripping
# watermark (the watermark check itself is covered by `disk_watermark=0.0`), no retry wait.
@pytest.fixture
def healthy_options(
    gateway: ScriptedGateway, tmp_path: Path, probe_command: CommandRuntime
) -> HealthOptions:
    return HealthOptions(
        gateway_url=gateway.url,
        rules=HealthRules(agent_min=1, check_schema=True, disk_watermark=1.0),
        provider=provider_off(),
        source_root=None,
        editable_allowed_roots=(),
        data_plane=dataclasses.replace(
            data_plane_config_from_boot(probe_command.config), walg_config_file=None
        ),
        data_volume=tmp_path,
        checkout_refusal=None,
        liveness_attempts=3,
        liveness_retry_interval_s=0.0,
    )


# meiva:note
# Write the root manifest a `meiva start` of just these services would have left in `home`.
def launch_record(home: Path, *sessions: str) -> None:
    chosen = [spec for spec in build_services() if spec.session in sessions]
    manifest = manifests.build_manifest(
        capabilities=("gateway", "agent-runner"),
        specs=chosen,
        environments={spec.session: {} for spec in chosen},
    )
    manifests.write_manifest(root_manifests_path(home), manifest)


# meiva:note
# A gateway host whose home is `home`, with an inert program finder: a probe round reaches
# only the real resources of the test.
@pytest.fixture
def probe_command(command_runtime: CommandRuntime, tmp_path: Path) -> CommandRuntime:
    command_runtime.machine.set(role="gateway")
    return dataclasses.replace(command_runtime, home=tmp_path)


# meiva:note
# A real git checkout with one committed file at `path`, standing in for the prod source.
def git_checkout(path: Path) -> Path:
    path.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "t@t"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "tracked.py").write_text("x = 1\n")
    for args in (("add", "."), ("commit", "-m", "init")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


def live_agent(conn: psycopg.Connection[Any], *, status: str = "idling") -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, %s, 'probe-test')",
        (agent_id, status),
    )
    conn.commit()
    return agent_id


# meiva:note
# A database handle whose cluster does not answer: the access URL names a loopback port nobody
# listens on, so every dial is refused, as when the data plane is down. It asks for the suite's
# Postgres only so the suite's guard admits a test that dials Postgres at all.
@pytest.fixture
def unreachable_database(db_url: str, database_gate: ProcessDbGate) -> Callable[[], Database]:
    closed = closed_port_url().replace("http://", "postgresql://meiva@")
    dead = Database(
        dataclasses.replace(db_config_from_settings(), db_url=f"{closed}/meiva"),
        gate=database_gate,
    )
    return lambda: dead


@pytest.fixture
def ran(emitted_events: EmittedEvents) -> FollowedAttributes:
    """The attributes of every `health_probe_ran` heartbeat the probe emits."""
    return emitted_events.following("health_probe_ran")


@pytest.fixture
def signals(emitted_events: EmittedEvents) -> FollowedAttributes:
    """The attributes of every `health_probe_failing` event the probe emits."""
    return emitted_events.following("health_probe_failing")


@pytest.fixture
def entry_command(
    db_url: str,
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    probe_command: CommandRuntime,
    gateway: ScriptedGateway,
) -> CommandRuntime:
    """A gateway host whose own configuration names the test's gateway and a floor of zero
    agents, with a prod checkout and an empty launch record under its home."""
    probe_command.config.set_field("gateway_url", gateway.url)
    probe_command.config.set_field("health_probe_agent_min", 0)
    source = git_checkout(probe_command.home / "source")
    launch_record(probe_command.home)
    return dataclasses.replace(probe_command, checkout=source)
