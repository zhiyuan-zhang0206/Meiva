# meiva:link docs/decisions/observability/telemetry/2026-11-24-cluster-routing-identity.md

from __future__ import annotations

import dataclasses
import re

from base.cluster import cluster_id
from base.host.db_authority.api import ensure_telemetry_token
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    GATEWAY,
    GATEWAY_INGRESS,
    RUNNER,
    CollectorUnit,
    mapping_at,
    sequence_at,
)
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    collector_unit as collector_unit,
)
from cli.commands.observability.tests.observability_inputs import (
    TELEMETRY_TOKEN,
    install_unit_capability,
    observability,
    write_cluster_record,
)
from cli.database.tests.command_inputs import command_runtime as command_runtime

OTHER_TOKEN = "fixture-" + "o" * 40
_ID_PATTERN = re.compile(r'IsMatch\(resource\.attributes\["meiva\.cluster"\], "([^"]+)"\)')


def _unit_with_token(unit: CollectorUnit, parent: str, token: str) -> CollectorUnit:
    home = unit.home.parent / parent / ".meiva"
    home.parent.mkdir()
    write_cluster_record(home)
    ensure_telemetry_token(home.resolve(), token)
    return dataclasses.replace(unit, home=home)


def _routing_pattern(unit: CollectorUnit, roles: frozenset[str] = GATEWAY) -> str:
    config = unit.render(roles, observability=observability(gateway_otlp_endpoint=GATEWAY_INGRESS))
    conditions = sequence_at(
        mapping_at(config, "processors", "filter/cluster_allow"), "logs", "log_record"
    )
    match = _ID_PATTERN.search(str(conditions[0]))
    assert match is not None
    return match.group(1)


def test_same_named_homes_of_different_clusters_filter_each_others_records(
    collector_unit: CollectorUnit,
) -> None:
    home_a = _unit_with_token(collector_unit, "a", TELEMETRY_TOKEN)
    home_b = _unit_with_token(collector_unit, "b", OTHER_TOKEN)
    assert home_a.home.name == home_b.home.name == ".meiva"

    pattern_a, pattern_b = _routing_pattern(home_a), _routing_pattern(home_b)

    assert re.search(pattern_a, cluster_id(TELEMETRY_TOKEN))
    assert re.search(pattern_b, cluster_id(OTHER_TOKEN))
    assert not re.search(pattern_a, cluster_id(OTHER_TOKEN))
    assert not re.search(pattern_b, cluster_id(TELEMETRY_TOKEN))


def test_every_unit_of_one_cluster_routes_under_the_same_identity(
    collector_unit: CollectorUnit,
) -> None:
    gateway = _unit_with_token(collector_unit, "gateway", TELEMETRY_TOKEN)
    runner_home = collector_unit.home.parent / "runner" / "not-named-meiva"
    runner_home.parent.mkdir()
    install_unit_capability(runner_home, telemetry=TELEMETRY_TOKEN)
    runner = dataclasses.replace(collector_unit, home=runner_home)

    assert _routing_pattern(runner, RUNNER) == _routing_pattern(gateway)


def test_a_unit_without_a_telemetry_token_admits_any_routing_identity(
    collector_unit: CollectorUnit,
) -> None:
    collector_unit.retract_token()

    pattern = _routing_pattern(collector_unit)

    assert pattern == ".*"
    assert re.search(pattern, cluster_id(OTHER_TOKEN))
