# meiva:note
# A real low-population query cannot turn an explicitly disabled agent host into a rollback.

import subprocess
from pathlib import Path

import psycopg
import pytest

from base import paths
from base.agents.context.clients import DatabaseFactory
from base.db.tests.fakes import patch_database
from base.telemetry.tests.capture.event_capture import EmittedEvents, FollowedAttributes
from cli.commands.cluster import health as cluster_health
from cli.commands.cluster.tests.health_probe_inputs import (
    Probe,
)
from cli.commands.cluster.tests.health_probe_inputs import emitted_events as emitted_events
from cli.commands.cluster.tests.health_probe_inputs import probe as probe
from cli.commands.cluster.tests.health_probe_inputs import (
    provider_guard_healthy as provider_guard_healthy,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    provider_guard_reports as provider_guard_reports,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    quiet_host_health as quiet_host_health,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    quiet_schema_health as quiet_schema_health,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    quiet_service_health as quiet_service_health,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    schema_verdicts as schema_verdicts,
)
from cli.commands.cluster.tests.health_probe_inputs import signals as signals
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database


def _launched_without(names: set[str]) -> None:
    # meiva:note
    # The last start launched agent-host and frontend, minus `names` (its root manifest).
    from services.supervision.meiva_root_glue.manifests import generate
    from services.supervision.roster import build_services

    launched = {"agent-host", "frontend"} - names
    generate(
        paths.root_manifests_path(paths.meiva_home()),
        capabilities=["gateway", "agent-runner"],
        specs=[spec for spec in build_services() if spec.session in launched],
    )


def _no_init(**_kwargs: object) -> None:
    return None


@pytest.fixture
def probe_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    quiet_schema_health: None,
    quiet_service_health: None,
    quiet_host_health: None,
) -> Path:
    """Keep the real DB/count/journal paths; intercept only external side effects.

    The checks after the population one are stubbed quiet: every check now runs
    on every round (task #5064), and the subject here is the population verdict."""
    assert db_conn.execute("SELECT count(*) FROM agents_meta").fetchone() == (0,)
    monkeypatch.setattr(paths, "meiva_home", lambda: tmp_path)
    monkeypatch.setattr(cluster_health, "gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(cluster_health, "disk_usage_failure", lambda: None)
    monkeypatch.setattr(cluster_health.telemetry, "init_telemetry", _no_init)
    return tmp_path


@pytest.fixture
def rollbacks(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.fixture
def alerts(emitted_events: EmittedEvents) -> FollowedAttributes:
    """The attributes of every `health_probe_failing` event the probe emits."""
    return emitted_events.following("health_probe_failing")


def test_expected_low_population_does_not_rollback_or_promote(
    probe: Probe,
    operator_database: DatabaseFactory,
    probe_home: Path,
    rollbacks: list[list[str]],
    alerts: FollowedAttributes,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _launched_without({"agent-host"})
    marker = paths.root_manifests_path(probe_home)
    intent_before = marker.read_bytes()
    assert cluster_health.agent_population(1, database_factory=operator_database) is False
    assert probe() == 1

    assert rollbacks == []
    # Local intent cannot hide a real global population outage: the signal is
    # still emitted, carrying the maintenance class for the reader.
    assert len(alerts) == 1
    assert alerts[0]["check"] == "agent_population"
    assert alerts[0]["failure_class"] == "maintenance"
    assert marker.read_bytes() == intent_before
    assert "maintenance" in capsys.readouterr().err


@pytest.mark.parametrize("intent", ["absent", "other-service"])
def test_unexpected_low_population_stays_unhealthy_without_release_mutation(
    probe: Probe,
    probe_home: Path,
    rollbacks: list[list[str]],
    alerts: FollowedAttributes,
    intent: str,
) -> None:
    if intent == "other-service":
        _launched_without({"frontend"})

    assert probe() == 1
    assert rollbacks == []


def test_reenabled_host_keeps_low_population_unhealthy(
    probe: Probe, probe_home: Path, rollbacks: list[list[str]], alerts: FollowedAttributes
) -> None:
    _launched_without({"agent-host"})
    assert probe() == 1
    assert rollbacks == []

    _launched_without(set())
    assert probe() == 1
    assert rollbacks == []


def test_disabled_host_does_not_explain_gateway_code_failure(
    probe: Probe,
    probe_home: Path,
    rollbacks: list[list[str]],
    alerts: FollowedAttributes,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: DatabaseFactory,
) -> None:
    _launched_without({"agent-host"})
    monkeypatch.setattr(cluster_health, "gateway_liveness_with_retry", lambda: False)

    def _plane_abnormal(*, database_factory: DatabaseFactory) -> bool:
        assert database_factory is operator_database
        return False

    monkeypatch.setattr(cluster_health, "data_plane_abnormal", _plane_abnormal)

    assert probe() == 1
    assert rollbacks == []


def test_maintenance_does_not_turn_db_failure_into_an_expected_population(
    probe: Probe,
    operator_database: DatabaseFactory,
    probe_home: Path,
    rollbacks: list[list[str]],
    alerts: FollowedAttributes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _launched_without({"agent-host"})

    def down(**_kwargs: object) -> None:
        raise ConnectionError("private test data plane unavailable")

    patch_database(monkeypatch, connect=down)
    assert (
        cluster_health.agent_population_failure_class(1, database_factory=operator_database)
        == "environment"
    )
    assert probe() == 1
    assert rollbacks == []
