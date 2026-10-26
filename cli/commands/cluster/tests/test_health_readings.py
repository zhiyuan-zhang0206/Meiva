# meiva:note
# How a health round observes the cluster, against the real things it observes.
#
# Each reader is run against a listener on a loopback port, the suite's throwaway Postgres and
# Redis, a real git checkout, a root manifest on disk or a path that does not exist; the
# reading it returns is asserted as a value. Whether a reading is healthy is decided in
# `test_health_verdicts.py`; the whole round is `test_cluster_health.py`.

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from base.cluster.machine import GatewayApiBaseMissing
from base.db import Database
from base.host.net.tests.local_http import closed_port_url
from base.paths import root_manifests_path
from cli.commands.cluster.health_readings import (
    HealthOptions,
    data_plane_abnormal,
    disk_usage_fraction,
    gateway_alive,
    read_editable_violations,
    read_gateway,
    read_launch_record,
    read_population,
    read_schema_skew,
    read_service_failures,
    read_source_tree,
    read_walg,
)
from cli.commands.cluster.plans.health_verdicts import (
    GatewayReading,
    PopulationRead,
    PopulationReading,
    SourceTreeReading,
)
from cli.commands.cluster.plans.service_probe_plan import (
    Launched,
    LaunchUnreadable,
    NeverLaunched,
)
from cli.commands.cluster.tests.gateway_server import Reply, serving
from cli.commands.cluster.tests.health_probe_inputs import (
    gateway as gateway,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    healthy_options as healthy_options,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    launch_record,
    live_agent,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    probe_command as probe_command,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    unreachable_database as unreachable_database,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime

_HEALTH = ("GET", "/api/health")


def test_a_gateway_answering_200_is_alive() -> None:
    with serving({_HEALTH: Reply(body={})}) as gateway:
        assert gateway_alive(gateway.url) is True

    assert [seen.target for seen in gateway.requests] == ["/api/health"]


@pytest.mark.parametrize("status", [401, 404, 500, 503])
def test_a_gateway_answering_anything_but_200_is_not_alive(status: int) -> None:
    with serving({_HEALTH: Reply(status=status, body={})}) as gateway:
        assert gateway_alive(gateway.url) is False


def test_a_gateway_nobody_listens_for_is_not_alive_rather_than_an_error() -> None:
    assert gateway_alive(closed_port_url()) is False


def test_a_host_with_no_gateway_address_fails_fast() -> None:
    with pytest.raises(GatewayApiBaseMissing):
        gateway_alive("")


def test_a_gateway_that_recovers_inside_the_budget_is_alive_after_exactly_the_failures_before_it(
    healthy_options: HealthOptions,
    probe_command: CommandRuntime,
    operator_database: Callable[[], Database],
) -> None:
    # meiva:note
    # A brief data-plane restart window is healthy once liveness recovers: attempt 3 answers.
    answers = (Reply(status=503, body={}), Reply(status=503, body={}), Reply(body={}))
    with serving({_HEALTH: answers}) as gateway:
        options = dataclasses.replace(healthy_options, gateway_url=gateway.url)
        reading = read_gateway(options, probe_command.config, operator_database, probe_command.rng)

    assert reading == GatewayReading(alive=True, data_plane_abnormal=False)
    assert len(gateway.requests) == 3


def test_a_gateway_that_stays_dead_spends_the_budget_once_and_no_more(
    db_url: str,
    redis_url: str,
    healthy_options: HealthOptions,
    probe_command: CommandRuntime,
    operator_database: Callable[[], Database],
) -> None:
    with serving({_HEALTH: Reply(status=503, body={})}) as gateway:
        options = dataclasses.replace(healthy_options, gateway_url=gateway.url)
        reading = read_gateway(options, probe_command.config, operator_database, probe_command.rng)

    assert reading == GatewayReading(alive=False, data_plane_abnormal=False)
    assert len(gateway.requests) == 3


def test_a_live_gateway_is_not_followed_by_a_look_at_the_data_plane(
    healthy_options: HealthOptions,
    probe_command: CommandRuntime,
    unreachable_database: Callable[[], Database],
) -> None:
    # meiva:note
    # The data plane is read only to classify a dead gateway: with the gateway answering, an
    # unreachable database is the population check's business.
    reading = read_gateway(
        healthy_options, probe_command.config, unreachable_database, probe_command.rng
    )

    assert reading == GatewayReading(alive=True, data_plane_abnormal=False)


def test_the_data_plane_is_normal_when_postgres_and_redis_answer(
    db_url: str,
    redis_url: str,
    probe_command: CommandRuntime,
    operator_database: Callable[[], Database],
) -> None:
    assert (
        data_plane_abnormal(
            probe_command.config, database_factory=operator_database, rng=probe_command.rng
        )
        is False
    )


def test_the_data_plane_is_abnormal_when_postgres_does_not_answer(
    redis_url: str,
    probe_command: CommandRuntime,
    unreachable_database: Callable[[], Database],
) -> None:
    assert (
        data_plane_abnormal(
            probe_command.config, database_factory=unreachable_database, rng=probe_command.rng
        )
        is True
    )


def test_the_data_plane_is_abnormal_when_redis_does_not_answer(
    db_url: str,
    redis_url: str,
    probe_command: CommandRuntime,
    operator_database: Callable[[], Database],
) -> None:
    probe_command.config.set_field("redis_url", closed_port_url().replace("http", "redis") + "/0")

    assert (
        data_plane_abnormal(
            probe_command.config, database_factory=operator_database, rng=probe_command.rng
        )
        is True
    )


def test_an_empty_cluster_counts_zero_agents(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    reading = read_population(operator_database, NeverLaunched())

    assert reading == PopulationReading(PopulationRead.COUNTED, 0, NeverLaunched())


def test_running_and_idling_agents_count_and_the_launch_record_rides_along(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    live_agent(db_conn, status="idling")
    live_agent(db_conn, status="running")
    launched = Launched(frozenset({"agent-host"}))

    assert read_population(operator_database, launched) == PopulationReading(
        PopulationRead.COUNTED, 2, launched
    )


def test_terminated_agents_and_agents_on_an_offline_machine_do_not_count(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    live_agent(db_conn)
    live_agent(db_conn, status="terminated")
    offline = live_agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET liveness_state = 'offline' WHERE id = %s", (offline,))
    db_conn.commit()

    assert read_population(operator_database, NeverLaunched()).count == 1


def test_an_unreachable_database_is_its_own_reading_not_a_zero(
    unreachable_database: Callable[[], Database],
) -> None:
    reading = read_population(unreachable_database, NeverLaunched())

    assert reading.read is PopulationRead.DB_UNREACHABLE


def test_a_schema_that_matches_this_checkout_is_not_a_skew(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    assert read_schema_skew(operator_database) is False


def test_a_database_with_a_migration_this_checkout_lacks_is_a_skew(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    name = "29991231T000000_from-the-future"
    db_conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    db_conn.commit()
    try:
        assert read_schema_skew(operator_database) is True
    finally:
        db_conn.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))
        db_conn.commit()


def test_a_database_that_cannot_be_reached_is_not_a_schema_failure(
    unreachable_database: Callable[[], Database],
) -> None:
    # meiva:note
    # A transient pgbouncer blip must not fire a false schema alert while code and DB are in
    # sync (2026-08-03): the connection is the precondition for reading the applied set.
    assert read_schema_skew(unreachable_database) is False


def test_a_home_no_start_has_launched_reads_never_launched(tmp_path: Path) -> None:
    assert read_launch_record(tmp_path) == NeverLaunched()


def test_the_launch_record_is_the_sessions_of_the_last_starts_manifest(tmp_path: Path) -> None:
    launch_record(tmp_path, "agent-host", "frontend")

    assert read_launch_record(tmp_path) == Launched(frozenset({"agent-host", "frontend"}))


def test_a_corrupt_manifest_is_an_unreadable_record_not_a_crash(tmp_path: Path) -> None:
    path = root_manifests_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    record = read_launch_record(tmp_path)

    assert isinstance(record, LaunchUnreadable)
    assert record.detail


def test_services_of_a_host_whose_role_is_unresolved_cannot_be_certified(
    probe_command: CommandRuntime,
) -> None:
    probe_command.config.set_field("machine_serve_gateway", False)
    probe_command.config.set_field("machine_serve_agent_runner", False)
    probe_command.config.set_field("machine_serve_observability_station", False)
    probe_command.machine.reset()
    probe_command.machine.set(name="unit-test-machine", host="localhost", description=None)

    assert read_service_failures(probe_command, Launched(frozenset())) == (
        "service roster unavailable",
    )


def test_services_of_a_unit_no_start_has_launched_cannot_be_certified(
    probe_command: CommandRuntime,
) -> None:
    assert read_service_failures(probe_command, NeverLaunched()) == (
        "launched services unavailable (no start has launched this unit)",
    )


def test_a_launch_record_that_cannot_be_read_cannot_certify_the_services(
    probe_command: CommandRuntime,
) -> None:
    assert read_service_failures(probe_command, LaunchUnreadable("corrupt manifest")) == (
        "launched services unavailable (corrupt manifest)",
    )


def test_a_unit_that_launched_nothing_has_no_failing_service(
    probe_command: CommandRuntime,
) -> None:
    assert read_service_failures(probe_command, Launched(frozenset())) == ()


def test_a_launched_service_nobody_answers_for_fails_the_probe(
    probe_command: CommandRuntime,
) -> None:
    failing = read_service_failures(probe_command, Launched(frozenset({"frontend"})))

    assert len(failing) == 1 and failing[0].startswith("frontend")


def test_a_service_the_capability_gate_drops_is_not_this_hosts_to_certify(
    probe_command: CommandRuntime,
) -> None:
    # meiva:note
    # The browser needs a display and a Chrome this inert host does not have: the roster gates
    # it out WITH a reason, so a start that recorded it launched still does not count it
    # against the cluster.
    probe_command.machine.set(role="gateway,agent-runner")

    assert read_service_failures(probe_command, Launched(frozenset({"browser"}))) == ()


def test_the_disk_reading_is_the_used_fraction_of_a_real_volume(tmp_path: Path) -> None:
    fraction = disk_usage_fraction(tmp_path)

    assert fraction is not None and 0.0 < fraction <= 1.0


def test_a_volume_that_cannot_be_measured_is_none_never_a_full_disk(tmp_path: Path) -> None:
    assert disk_usage_fraction(tmp_path / "no-such-volume") is None


def test_wal_archiving_that_is_off_is_neither_failing_nor_enabled() -> None:
    assert read_walg(None) == (None, False)


def test_a_wal_g_configuration_that_does_not_exist_is_a_failure_without_dialing_postgres(
    tmp_path: Path,
) -> None:
    failure, enabled = read_walg(tmp_path / "walg.json")

    assert enabled is True
    assert failure == "WAL archiving: the WAL-G configuration is unusable"


def test_there_is_nothing_to_inspect_without_a_prod_checkout() -> None:
    assert read_editable_violations(None, ()) == ()
    assert read_source_tree(None) == SourceTreeReading(guard_skipped=False, findings=())


def test_a_half_uninstalled_editable_pointer_is_listed_and_never_rewritten(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    pth = source / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_meiva.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(source))

    violations = read_editable_violations(source, ())

    assert len(violations) == 1 and str(pth) in violations[0]
    assert pth.read_text() == str(source)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real git checkout with one committed file, standing in for the prod source."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_a_clean_checkout_has_no_source_tree_findings(checkout: Path) -> None:
    assert read_source_tree(checkout) == SourceTreeReading(guard_skipped=False, findings=())


def test_an_untracked_file_outside_the_whitelist_is_tampering(checkout: Path) -> None:
    (checkout / "junk.txt").write_text("edited in place")

    assert read_source_tree(checkout) == SourceTreeReading(
        guard_skipped=False, findings=("untracked outside whitelist: junk.txt",)
    )


def test_an_edited_tracked_file_is_tampering(checkout: Path) -> None:
    (checkout / "tracked.py").write_text("x = 2\n")

    reading = read_source_tree(checkout)

    assert reading.guard_skipped is False
    assert reading.findings == ("tracked change: M tracked.py",)


def test_a_directory_that_is_not_a_git_checkout_is_a_blind_guard_not_a_clean_tree(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    assert read_source_tree(plain) == SourceTreeReading(
        guard_skipped=True, findings=("not a git checkout",)
    )
