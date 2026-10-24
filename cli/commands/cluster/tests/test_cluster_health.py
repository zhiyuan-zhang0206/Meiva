# meiva:note
# `meiva cluster health-probe` end to end: a whole round against a real cluster at test scale.
#
# The gateway is a loopback listener, the data plane the suite's throwaway Postgres and Redis,
# the prod checkout a real git repository, the home a directory under `tmp_path` with a root
# manifest, and the events the round emits are read from the capture pipeline. A fault is made
# the way it happens: a gateway that answers 503, an agent table with nobody in it, an edited
# file in the checkout. What each check says and how a failure is classed is decided from values
# (`test_health_verdicts.py`) and how each reading is taken is `test_health_readings.py`; this
# file proves the shell wires them: the exit status, the streams, and the one event per failed
# check plus the heartbeat.

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.db import Database
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.telemetry import EventPipeline
from base.telemetry.tests.capture.event_capture import EmittedEvents, FollowedAttributes
from cli.commands.cluster.health import cmd_health_probe, health_options, run_health_probe
from cli.commands.cluster.health_readings import HealthOptions
from cli.commands.cluster.plans.health_verdicts import HealthRules
from cli.commands.cluster.tests.gateway_server import Reply, serving
from cli.commands.cluster.tests.health_probe_inputs import (
    emitted_events as emitted_events,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    entry_command as entry_command,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    gateway as gateway,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    git_checkout,
    launch_record,
    live_agent,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    healthy_options as healthy_options,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    probe_command as probe_command,
)
from cli.commands.cluster.tests.health_probe_inputs import ran as ran
from cli.commands.cluster.tests.health_probe_inputs import signals as signals
from cli.commands.cluster.tests.health_probe_inputs import (
    unreachable_database as unreachable_database,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime

_HEALTH = ("GET", "/api/health")
_DECLARED = {"health_probe_ran", "health_probe_failing"}


# meiva:note
# One cluster the probe can be run against: a gateway host with a live agent and a start that
# launched nothing, and the options of a round that holds it to its rules. A test changes the
# options or the cluster, then calls `probe`.
@dataclass
class Cluster:
    command: CommandRuntime
    options: HealthOptions
    database: Callable[[], Database]
    producer: Callable[[], EventPipeline]
    conn: psycopg.Connection

    def probe(self, **changes: Any) -> int:
        options = dataclasses.replace(self.options, **changes)
        return run_health_probe(
            options,
            database_factory=self.database,
            producer=self.producer,
            command=self.command,
        )


@pytest.fixture
def cluster(
    db_url: str,
    redis_url: str,
    db_conn: psycopg.Connection,
    probe_command: CommandRuntime,
    healthy_options: HealthOptions,
    operator_database: Callable[[], Database],
    emitted_events: EmittedEvents,
) -> Iterator[Cluster]:
    live_agent(db_conn)
    launch_record(probe_command.home)
    yield Cluster(
        probe_command, healthy_options, operator_database, emitted_events.producer, db_conn
    )
    # meiva:note
    # Whatever a round emits is one of the two events the probe declares, with the declared
    # category and level.
    for event in emitted_events:
        assert event.category == "telemetry"
        assert event.event_name in _DECLARED
        assert event.level == ("info" if event.event_name == "health_probe_ran" else "warning")


def _checks(signals: FollowedAttributes) -> list[object]:
    return [signal["check"] for signal in signals]


def test_a_healthy_cluster_exits_0_prints_every_check_and_only_beats(
    cluster: Cluster,
    capsys: pytest.CaptureFixture[str],
    ran: FollowedAttributes,
    signals: FollowedAttributes,
) -> None:
    assert cluster.probe() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "  ✓ gateway liveness",
        "  ✓ agent population (>= 1)",
        "  ✓ schema health",
        "  ✓ service probes",
        "  ✓ disk usage",
        "  ✓ editable install records",
        "  ✓ source tree integrity",
        "  ✓ provider account guard",
    ]
    assert ran == [{"unhealthy_checks": 0}]
    assert signals == []


# meiva:note
# `health_probe_ran` is the dead-man heartbeat: one per run on every path, carrying the
# unhealthy-check count; the failing event is separate.
def test_every_run_emits_the_heartbeat_healthy_or_not(
    cluster: Cluster, ran: FollowedAttributes, signals: FollowedAttributes
) -> None:
    assert cluster.probe() == 0
    launch_record(cluster.command.home, "frontend")
    assert cluster.probe() == 1
    with serving({_HEALTH: Reply(status=503, body={})}) as dead:
        assert cluster.probe(gateway_url=dead.url) == 1

    assert ran == [{"unhealthy_checks": 0}, {"unhealthy_checks": 1}, {"unhealthy_checks": 2}]
    assert _checks(signals) == ["service_probe", "gateway_liveness", "service_probe"]


def test_a_refused_checkout_never_observes_the_cluster_and_emits_no_heartbeat(
    cluster: Cluster,
    capsys: pytest.CaptureFixture[str],
    ran: FollowedAttributes,
    signals: FollowedAttributes,
) -> None:
    # meiva:note
    # A wrong-checkout refusal (exit 2) is not a run: a worktree driving the probe is the
    # 2026-08-07 accident (task #1025), and the cron log shows the distinct exit code.
    assert cluster.probe(checkout_refusal="dev checkout") == 2

    assert capsys.readouterr().err == "health-probe refused: dev checkout\n"
    assert ran == [] and signals == []


def test_a_dead_gateway_over_a_working_data_plane_is_a_code_class_failure(
    cluster: Cluster, signals: FollowedAttributes
) -> None:
    with serving({_HEALTH: Reply(status=503, body={})}) as dead:
        assert cluster.probe(gateway_url=dead.url) == 1

    assert len(dead.requests) == 3
    assert signals == [
        {
            "check": "gateway_liveness",
            "failure_class": "code",
            "message": "FAIL: gateway liveness — health endpoint unreachable or non-200",
        }
    ]


def test_a_dead_gateway_over_a_dead_data_plane_is_an_environment_failure_that_says_so(
    cluster: Cluster,
    unreachable_database: Callable[[], Database],
    capsys: pytest.CaptureFixture[str],
    signals: FollowedAttributes,
) -> None:
    # meiva:note
    # A data-plane failure signals but cannot launch an unrelated code rollback.
    cluster.database = unreachable_database
    with serving({_HEALTH: Reply(status=503, body={})}) as dead:
        assert cluster.probe(gateway_url=dead.url) == 1

    assert signals[0]["check"] == "gateway_liveness"
    assert signals[0]["failure_class"] == "environment"
    assert "  environment-class failure" in capsys.readouterr().err


def test_a_gateway_that_answers_on_the_third_attempt_is_healthy(
    cluster: Cluster, signals: FollowedAttributes, ran: FollowedAttributes
) -> None:
    answers = (Reply(status=503, body={}), Reply(status=503, body={}), Reply(body={}))
    with serving({_HEALTH: answers}) as flapping:
        assert cluster.probe(gateway_url=flapping.url) == 0

    assert len(flapping.requests) == 3
    assert signals == [] and ran == [{"unhealthy_checks": 0}]


def test_an_empty_cluster_fails_the_population_floor_with_the_class_local_intent_explains(
    cluster: Cluster,
    capsys: pytest.CaptureFixture[str],
    signals: FollowedAttributes,
) -> None:
    # meiva:note
    # Local intent cannot hide a real global outage: the last start launched no agent host, so
    # the signal carries the maintenance class for the reader, and it is still emitted.
    cluster.conn.execute("UPDATE agents_meta SET status = 'terminated'")
    cluster.conn.commit()

    assert cluster.probe() == 1

    assert signals == [
        {
            "check": "agent_population",
            "failure_class": "maintenance",
            "message": "FAIL: agent population — fewer than 1 agent(s) running/idling",
        }
    ]
    assert "local agent maintenance explains the low population" in capsys.readouterr().err


def test_an_empty_cluster_whose_start_launched_the_agent_host_is_a_code_failure(
    cluster: Cluster, signals: FollowedAttributes
) -> None:
    cluster.conn.execute("UPDATE agents_meta SET status = 'terminated'")
    cluster.conn.commit()
    launch_record(cluster.command.home, "agent-host")

    assert cluster.probe() == 1

    population = next(s for s in signals if s["check"] == "agent_population")
    assert population["failure_class"] == "code"


def test_a_reenabled_host_keeps_a_low_population_unhealthy(
    cluster: Cluster, signals: FollowedAttributes
) -> None:
    cluster.conn.execute("UPDATE agents_meta SET status = 'terminated'")
    cluster.conn.commit()
    assert cluster.probe() == 1
    launch_record(cluster.command.home, "agent-host")
    assert cluster.probe() == 1

    populations = [s for s in signals if s["check"] == "agent_population"]
    assert [s["failure_class"] for s in populations] == ["maintenance", "code"]


def test_an_unreachable_database_is_an_environment_class_population_failure_not_a_schema_one(
    cluster: Cluster,
    unreachable_database: Callable[[], Database],
    capsys: pytest.CaptureFixture[str],
    signals: FollowedAttributes,
) -> None:
    # meiva:note
    # Maintenance intent does not turn a database failure into an expected population: with
    # the agent table unreachable the class is the environment's, and the schema check stays
    # silent because a connection flake is not a skew.
    cluster.database = unreachable_database

    assert cluster.probe() == 1

    population = next(s for s in signals if s["check"] == "agent_population")
    assert population["failure_class"] == "environment"
    assert "schema" not in _checks(signals)
    assert "  environment-class failure" in capsys.readouterr().err


def test_an_agent_floor_of_zero_is_met_by_an_empty_cluster(cluster: Cluster) -> None:
    cluster.conn.execute("UPDATE agents_meta SET status = 'terminated'")
    cluster.conn.commit()

    assert cluster.probe(rules=HealthRules(0, check_schema=True, disk_watermark=0.9)) == 0


def test_a_schema_skew_fails_the_round_and_a_disabled_check_does_not(
    cluster: Cluster, capsys: pytest.CaptureFixture[str], signals: FollowedAttributes
) -> None:
    name = "29991231T000000_from-the-future"
    cluster.conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    cluster.conn.commit()
    try:
        assert cluster.probe() == 1
        failing = capsys.readouterr()
        skip = HealthRules(1, check_schema=False, disk_watermark=0.9)
        assert cluster.probe(rules=skip) == 0
    finally:
        cluster.conn.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))
        cluster.conn.commit()

    assert "FAIL: schema health" in failing.err
    assert signals == [
        {
            "check": "schema",
            "failure_class": "code",
            "message": "FAIL: schema health — applied version behind required (CodeBehindSchema)",
        }
    ]


# meiva:note
# A dead selected service fails the probe and emits its failing check.
def test_a_launched_service_nobody_answers_for_fails_the_probe_alert_only(
    cluster: Cluster, signals: FollowedAttributes
) -> None:
    launch_record(cluster.command.home, "frontend")

    assert cluster.probe() == 1

    assert len(signals) == 1
    assert signals[0]["check"] == "service_probe"
    assert signals[0]["failure_class"] == "alert-only"
    assert str(signals[0]["message"]).startswith("FAIL: service probe — not healthy: frontend")


# meiva:note
# Rolling back code frees no disk space (the 2026-08-08 outage class: checkpoint growth filled
# the disk and the gateway could not start). A watermark of zero is exceeded by any real volume.
def test_a_data_volume_over_the_watermark_fails_first_and_is_alert_only(
    cluster: Cluster,
    capsys: pytest.CaptureFixture[str],
    signals: FollowedAttributes,
) -> None:
    over = HealthRules(1, check_schema=True, disk_watermark=0.0)
    with serving({_HEALTH: Reply(status=503, body={})}) as dead:
        assert cluster.probe(rules=over, gateway_url=dead.url) == 1

    assert _checks(signals) == ["disk_usage", "gateway_liveness"]
    assert signals[0]["failure_class"] == "alert-only"
    err = capsys.readouterr().err
    assert err.index("FAIL: disk usage") < err.index("FAIL: gateway liveness")
    assert "(watermark 0%)" in err


# meiva:note
# The 2026-08-28 outage class: edited source broke `import meiva` for every agent on the box.
# Rolling back code does not undo an on-disk edit.
def test_a_tampered_prod_checkout_fails_the_probe_alert_only(
    cluster: Cluster, tmp_path: Path, signals: FollowedAttributes
) -> None:
    source = git_checkout(tmp_path / "prod-source")
    (source / "junk.txt").write_text("edited in place")

    assert cluster.probe(source_root=source) == 1

    assert len(signals) == 1
    assert signals[0]["check"] == "source_tree"
    assert "junk.txt" in str(signals[0]["message"])


def test_a_clean_prod_checkout_passes(cluster: Cluster, tmp_path: Path) -> None:
    assert cluster.probe(source_root=git_checkout(tmp_path / "prod-source")) == 0


def test_a_broken_wal_archiver_configuration_fails_the_probe_alert_only(
    cluster: Cluster, tmp_path: Path, signals: FollowedAttributes
) -> None:
    plane = dataclasses.replace(
        cluster.options.data_plane, walg_config_file=tmp_path / "missing-walg.json"
    )

    assert cluster.probe(data_plane=plane) == 1

    assert signals == [
        {
            "check": "walg_archive",
            "failure_class": "alert-only",
            "message": "FAIL: WAL archiving: the WAL-G configuration is unusable",
        }
    ]


def test_a_low_provider_balance_and_halted_agents_both_report_in_the_same_run(
    cluster: Cluster,
    capsys: pytest.CaptureFixture[str],
    signals: FollowedAttributes,
    ran: FollowedAttributes,
) -> None:
    halted = live_agent(cluster.conn)
    cluster.conn.execute(
        "UPDATE agents_meta SET permanent_reject_streak = 2, last_turn_fatal_at = now() "
        "WHERE id = %s",
        (halted,),
    )
    cluster.conn.commit()
    balance = {
        "is_available": True,
        "balance_infos": [{"currency": "CNY", "total_balance": "12.50"}],
    }
    with serving({("GET", "/user/balance"): Reply(body=balance)}) as provider:
        guard = dataclasses.replace(
            cluster.options.provider,
            balance_enabled=True,
            deepseek_api_key="sk-test",
            balance_url=f"{provider.url}/user/balance",
            blocked_agents_enabled=True,
            blocked_agents_min=1,
        )
        assert cluster.probe(provider=guard) == 1

    assert provider.requests[0].headers["authorization"] == "Bearer sk-test"
    assert _checks(signals) == ["provider_balance", "provider_blocked_agents"]
    assert ran == [{"unhealthy_checks": 2}]
    err = capsys.readouterr().err
    assert "(provider balance detail: ¥12.50" in err
    assert "(provider blocked-agents detail: 1 agent(s) halted" in err
    assert "12.50" not in str(signals[0]["message"])


def test_a_schema_failure_does_not_mask_the_provider_guard(
    cluster: Cluster, signals: FollowedAttributes
) -> None:
    # meiva:note
    # A schema/code skew used to end the round before the provider checks, so the balance
    # runway went fully silent exactly when the fleet needed it (2026-10-12 evaluation of task
    # #5064).
    name = "29991231T000000_from-the-future"
    cluster.conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    cluster.conn.commit()
    low = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "1"}]}
    try:
        with serving({("GET", "/user/balance"): Reply(body=low)}) as provider:
            guard = dataclasses.replace(
                cluster.options.provider,
                balance_enabled=True,
                deepseek_api_key="sk-test",
                balance_url=f"{provider.url}/user/balance",
            )
            assert cluster.probe(provider=guard) == 1
    finally:
        cluster.conn.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))
        cluster.conn.commit()

    assert _checks(signals) == ["schema", "provider_balance"]


# meiva:note
# The probe fires every few minutes; a persistent outage emits the signal on every run (a
# Grafana `for:` needs continuous samples), a healthy run emits nothing, and no state file is
# kept.
def test_a_persistent_outage_signals_every_run_and_a_healthy_run_signals_nothing(
    cluster: Cluster, signals: FollowedAttributes
) -> None:
    before = sorted(path.name for path in cluster.command.home.rglob("*"))
    with serving({_HEALTH: Reply(status=503, body={})}) as dead:
        assert cluster.probe(gateway_url=dead.url) == 1
        assert cluster.probe(gateway_url=dead.url) == 1
    assert _checks(signals) == ["gateway_liveness"] * 2

    assert cluster.probe() == 0
    assert len(signals) == 2
    assert sorted(path.name for path in cluster.command.home.rglob("*")) == before


def test_repeated_rounds_never_touch_the_home_beyond_reading_it(
    cluster: Cluster, tmp_path: Path
) -> None:
    # meiva:note
    # The probe observes and signals; it never selects a release, rolls back code or publishes
    # known-good state, so legacy bookkeeping files in the home are exactly as they were.
    legacy = {
        "health_probe_failures": "999\ncode\nold outage\n2026-09-01T00:00:00+00:00",
        "health_probe_pending_lkg_passes": "candidate\n999",
    }
    for name, content in legacy.items():
        (cluster.command.home / name).write_text(content)
    source = git_checkout(tmp_path / "prod-source")
    (source / "junk.txt").write_text("edited in place")

    with serving({_HEALTH: Reply(status=503, body={})}) as dead:
        for _round in range(3):
            assert cluster.probe(gateway_url=dead.url, source_root=source) == 1

    assert {name: (cluster.command.home / name).read_text() for name in legacy} == legacy


def test_the_command_reads_its_round_from_its_own_configuration(
    probe_command: CommandRuntime, tmp_path: Path
) -> None:
    probe_command.config.set_field("gateway_url", "http://gw.test:8000")
    probe_command.config.set_field("health_probe_agent_min", 0)

    options = health_options(probe_command, check_schema=False)

    assert options.gateway_url == "http://gw.test:8000"
    assert options.rules == HealthRules(agent_min=0, check_schema=False, disk_watermark=0.90)
    assert options.source_root == probe_command.home / "source"
    assert options.data_plane.walg_config_file is None
    assert options.checkout_refusal is None
    assert options.provider.balance_min_cny == 500.0


# meiva:note
# The facts of the invocation come from its runtime, not from the process: the user's home names
# the editable allowlist, the platform names the data volume, and the checkout is judged against
# the runtime's own home (a home that carries its `source` forbids any other checkout).
def test_the_round_takes_the_user_home_the_platform_and_the_checkout_from_the_invocation(
    probe_command: CommandRuntime, tmp_path: Path
) -> None:
    linux = health_options(probe_command, check_schema=True)
    mac = health_options(
        dataclasses.replace(probe_command, host=fake_host(platform=DARWIN_ARM64)),
        check_schema=True,
    )
    (probe_command.home / "source").mkdir()
    foreign = health_options(
        dataclasses.replace(probe_command, checkout=tmp_path / "elsewhere"), check_schema=True
    )

    assert linux.editable_allowed_roots == (probe_command.session.user_home / "Meiva",)
    assert linux.data_volume == Path("/")
    assert mac.data_volume == Path("/System/Volumes/Data")
    assert foreign.checkout_refusal is not None
    assert str(tmp_path / "elsewhere") in foreign.checkout_refusal


def test_the_command_runs_a_round_from_its_configuration(
    entry_command: CommandRuntime,
    operator_database: Callable[[], Database],
    emitted_events: EmittedEvents,
    capsys: pytest.CaptureFixture[str],
    ran: FollowedAttributes,
) -> None:
    # meiva:note
    # The disk line depends on the volume of the machine running the suite, so the exit status
    # is not asserted; the lines below come from the command's own configuration.
    rc = cmd_health_probe(
        check_schema=False,
        database_factory=operator_database,
        producer=emitted_events.producer,
        command=entry_command,
    )

    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert "  ✓ gateway liveness" in out
    assert "  ✓ agent population (>= 0)" in out
    assert "schema health" not in out
    assert len(ran) == 1
