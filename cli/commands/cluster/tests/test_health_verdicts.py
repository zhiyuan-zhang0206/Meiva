# meiva:note
# What the cluster health probe says about each check, decided from readings.
#
# `judge_health` takes what a round observed and returns, in the report's order, what every
# check prints and which failure it signals; these tests hold that judgment to the incidents
# it exists for. How the readings are taken is `test_health_readings.py`; the whole probe run
# against a real cluster is `test_cluster_health.py`.

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from cli.commands.cluster.plans.health_verdicts import (
    CheckResult,
    Failure,
    FailureClass,
    GatewayReading,
    HealthReadings,
    HealthRules,
    PopulationRead,
    PopulationReading,
    SourceTreeReading,
    judge_health,
    liveness_waits,
)
from cli.commands.cluster.plans.provider_verdicts import ProviderCheck
from cli.commands.cluster.plans.service_probe_plan import (
    Launched,
    LaunchRecord,
    LaunchUnreadable,
    NeverLaunched,
)

_RULES = HealthRules(agent_min=1, check_schema=True, disk_watermark=0.90)
_HEALTHY = HealthReadings(
    disk_fraction=0.42,
    gateway=GatewayReading(alive=True, data_plane_abnormal=False),
    population=PopulationReading(PopulationRead.COUNTED, 3, Launched(frozenset({"agent-host"}))),
    schema_skew=False,
    service_failures=(),
    walg_failure=None,
    walg_enabled=False,
    editable_violations=(),
    source_tree=SourceTreeReading(guard_skipped=False, findings=()),
    provider=(),
)


def _judge(rules: HealthRules = _RULES, **overrides: Any) -> tuple[CheckResult, ...]:
    return judge_health(replace(_HEALTHY, **overrides), rules)


def _checks(results: tuple[CheckResult, ...]) -> list[str]:
    return [result.check for result in results]


def _failures(results: tuple[CheckResult, ...]) -> dict[str, Failure]:
    return {r.check: r.failure for r in results if r.failure is not None}


def _failed(results: tuple[CheckResult, ...], check: str) -> CheckResult:
    return next(result for result in results if result.check == check)


def test_a_healthy_round_prints_every_check_in_order_and_signals_nothing() -> None:
    results = _judge()

    assert _checks(results) == [
        "gateway_liveness",
        "agent_population",
        "schema",
        "service_probe",
        "disk_usage",
        "editable_install",
        "source_tree",
        "provider_guard",
    ]
    assert [line for result in results for line in result.stdout] == [
        "  ✓ gateway liveness",
        "  ✓ agent population (>= 1)",
        "  ✓ schema health",
        "  ✓ service probes",
        "  ✓ disk usage",
        "  ✓ editable install records",
        "  ✓ source tree integrity",
        "  ✓ provider account guard",
    ]
    assert _failures(results) == {}
    assert all(result.stderr == () for result in results)


def test_the_failure_classes_are_the_names_grafana_groups_by() -> None:
    assert {c.value for c in FailureClass} == {"code", "environment", "maintenance", "alert-only"}


def test_a_full_data_volume_leads_the_report_and_is_alert_only() -> None:
    # meiva:note
    # The 2026-08-08 outage class: checkpoint growth filled the disk and the gateway could not
    # start. Rolling the cluster back frees no disk space, so it is not rollback evidence.
    results = _judge(disk_fraction=0.924, gateway=GatewayReading(False, False))

    assert _checks(results)[:2] == ["disk_usage", "gateway_liveness"]
    assert results[0].stderr == ("FAIL: disk usage — data volume 92.4% used (watermark 90%)",)
    assert results[0].failure == Failure(results[0].stderr[0], FailureClass.ALERT_ONLY)
    assert "disk_usage" not in _checks(results[1:])


@pytest.mark.parametrize(("fraction", "over"), [(0.90, False), (0.9001, True), (0.0, False)])
def test_exactly_at_the_watermark_is_healthy_and_strictly_over_alerts(
    fraction: float, over: bool
) -> None:
    assert ("disk_usage" in _failures(_judge(disk_fraction=fraction))) is over


def test_an_unmeasurable_disk_never_synthesizes_an_alarm() -> None:
    results = _judge(disk_fraction=None)

    assert _failures(results) == {}
    assert _failed(results, "disk_usage").stdout == ("  ✓ disk usage",)


def test_the_watermark_is_the_rules_not_a_constant() -> None:
    rules = replace(_RULES, disk_watermark=0.5)

    assert "(watermark 50%)" in _failures(_judge(rules, disk_fraction=0.6))["disk_usage"].message


def test_a_dead_gateway_over_a_healthy_data_plane_is_code_class() -> None:
    failure = _failures(_judge(gateway=GatewayReading(False, False)))["gateway_liveness"]

    assert failure.message == "FAIL: gateway liveness — health endpoint unreachable or non-200"
    assert failure.failure_class is FailureClass.CODE


def test_a_dead_gateway_over_an_abnormal_data_plane_is_environment_class_and_says_so() -> None:
    # meiva:note
    # A data-plane failure signals but cannot launch an unrelated code rollback.
    results = _judge(gateway=GatewayReading(False, True))

    gateway = _failed(results, "gateway_liveness")
    assert gateway.failure is not None
    assert gateway.failure.failure_class is FailureClass.ENVIRONMENT
    assert gateway.stderr[-1] == "  environment-class failure"


def _population(read: PopulationRead, count: int, launched: LaunchRecord) -> PopulationReading:
    return PopulationReading(read, count, launched)


@pytest.mark.parametrize(
    ("reading", "failure_class"),
    [
        (_population(PopulationRead.COUNTED, 0, Launched(frozenset({"agent-host"}))), "code"),
        (_population(PopulationRead.COUNTED, 0, Launched(frozenset({"frontend"}))), "maintenance"),
        (_population(PopulationRead.COUNTED, 0, NeverLaunched()), "maintenance"),
        (_population(PopulationRead.COUNTED, 0, LaunchUnreadable("corrupt")), "code"),
        (_population(PopulationRead.NO_ROW, 0, Launched(frozenset({"agent-host"}))), "code"),
        (_population(PopulationRead.DB_UNREACHABLE, 0, NeverLaunched()), "environment"),
    ],
    ids=[
        "agent host launched",
        "agent host not launched",
        "never launched",
        "launch record unreadable",
        "no row",
        "database unreachable",
    ],
)
def test_a_low_population_is_classed_by_what_explains_it(
    reading: PopulationReading, failure_class: str
) -> None:
    # meiva:note
    # Local intent cannot hide a real global outage: the signal is still emitted, carrying the
    # maintenance class for the reader. A failed population query is not evidence that the
    # running code regressed, and a launch record nobody can read explains nothing.
    failure = _failures(_judge(population=reading))["agent_population"]

    assert failure.message == "FAIL: agent population — fewer than 1 agent(s) running/idling"
    assert failure.failure_class.value == failure_class


def test_a_low_population_names_its_explanation_on_the_diagnostic_stream() -> None:
    maintenance = _population(PopulationRead.COUNTED, 0, NeverLaunched())
    environment = _population(PopulationRead.DB_UNREACHABLE, 0, NeverLaunched())
    code = _population(PopulationRead.COUNTED, 0, Launched(frozenset({"agent-host"})))

    assert _failed(_judge(population=maintenance), "agent_population").stderr[1:] == (
        "  local agent maintenance explains the low population",
    )
    assert _failed(_judge(population=environment), "agent_population").stderr[1:] == (
        "  environment-class failure",
    )
    assert _failed(_judge(population=code), "agent_population").stderr[1:] == ()


def test_the_agent_floor_is_the_rules_and_zero_is_always_met() -> None:
    empty = _population(PopulationRead.COUNTED, 0, NeverLaunched())

    assert _failed(_judge(population=empty), "agent_population").failure is not None
    quiet = _judge(replace(_RULES, agent_min=0), population=empty)
    assert _failed(quiet, "agent_population").stdout == ("  ✓ agent population (>= 0)",)
    at_floor = _population(PopulationRead.COUNTED, 2, NeverLaunched())
    assert (
        _failed(
            _judge(replace(_RULES, agent_min=2), population=at_floor), "agent_population"
        ).failure
        is None
    )


def test_a_real_schema_skew_is_code_class() -> None:
    failure = _failures(_judge(schema_skew=True))["schema"]

    assert failure.message == (
        "FAIL: schema health — applied version behind required (CodeBehindSchema)"
    )
    assert failure.failure_class is FailureClass.CODE


def test_a_disabled_schema_check_is_not_in_the_report() -> None:
    results = _judge(replace(_RULES, check_schema=False), schema_skew=True)

    assert "schema" not in _checks(results)
    assert _failures(results) == {}


def test_failing_services_are_listed_sorted_in_one_alert_only_message() -> None:
    failure = _failures(_judge(service_failures=("meiva-ops (down)", "frontend")))["service_probe"]

    assert failure.message == "FAIL: service probe — not healthy: frontend, meiva-ops (down)"
    assert failure.failure_class is FailureClass.ALERT_ONLY


def test_wal_archiving_is_silent_while_off_and_a_check_while_on() -> None:
    assert "walg_archive" not in _checks(_judge())
    on = _failed(_judge(walg_enabled=True), "walg_archive")
    assert on.stdout == ("  ✓ WAL archiving",) and on.failure is None


def test_a_broken_archiver_fails_with_its_own_message() -> None:
    results = _judge(walg_enabled=True, walg_failure="WAL archiving: the archiver is failing")

    failure = _failures(results)["walg_archive"]
    assert failure == Failure(
        "FAIL: WAL archiving: the archiver is failing", FailureClass.ALERT_ONLY
    )
    assert _failed(results, "walg_archive").stdout == ()


def test_a_polluted_editable_install_lists_its_violations() -> None:
    failure = _failures(_judge(editable_violations=("a.pth -> /tmp/x", "b is missing")))[
        "editable_install"
    ]

    assert failure.message == (
        "FAIL: editable install — prod venv editable install names non-allowlisted source or "
        "is inconsistent: a.pth -> /tmp/x; b is missing"
    )
    assert failure.failure_class is FailureClass.ALERT_ONLY


def test_a_tampered_source_tree_names_the_tamper() -> None:
    # meiva:note
    # The 2026-08-28 outage class: edited source broke `import meiva` for every agent on the
    # box. Rolling back code does not undo an on-disk edit.
    tree = SourceTreeReading(
        guard_skipped=False, findings=("untracked outside whitelist: junk.txt",)
    )

    failure = _failures(_judge(source_tree=tree))["source_tree"]

    assert failure.message == (
        "FAIL: source tree — prod source tree tampered: untracked outside whitelist: junk.txt"
    )
    assert failure.failure_class is FailureClass.ALERT_ONLY


def test_a_blind_source_tree_guard_is_a_distinct_alert_never_a_clean_tree() -> None:
    tree = SourceTreeReading(guard_skipped=True, findings=("git unavailable",))

    failure = _failures(_judge(source_tree=tree))["source_tree"]

    assert failure.message == "FAIL: source tree — prod source tree guard skipped: git unavailable"


def _provider(check: str, failure: str | None, *notes: str) -> ProviderCheck:
    return ProviderCheck(check, failure, notes)


def test_each_failing_provider_check_reports_with_its_detail_line_first() -> None:
    results = _judge(
        provider=(
            _provider("provider_balance", "FAIL: provider balance — low", "  (detail: ¥1.00)"),
            _provider("provider_blocked_agents", "FAIL: provider blocked agents — halted"),
        )
    )

    balance = _failed(results, "provider_balance")
    assert balance.stderr == ("  (detail: ¥1.00)", "FAIL: provider balance — low")
    assert balance.failure == Failure("FAIL: provider balance — low", FailureClass.ALERT_ONLY)
    assert _failed(results, "provider_blocked_agents").failure is not None
    assert "provider_guard" not in _checks(results)


def test_a_passing_provider_guard_prints_one_line_and_keeps_its_skip_notes() -> None:
    results = _judge(
        provider=(_provider("provider_balance", None, "  (provider balance check skipped: x)"),)
    )

    assert _failed(results, "provider_balance").stderr == ("  (provider balance check skipped: x)",)
    assert _failed(results, "provider_guard").stdout == ("  ✓ provider account guard",)
    assert _failures(results) == {}


def test_a_schema_skew_does_not_mask_the_provider_guard() -> None:
    # meiva:note
    # A schema/code skew used to end the round before the provider checks, so the balance
    # runway went fully silent exactly when the fleet needed it (2026-10-12 evaluation of task
    # #5064).
    results = _judge(
        schema_skew=True,
        provider=(_provider("provider_balance", "FAIL: provider balance — low"),),
    )

    assert list(_failures(results)) == ["schema", "provider_balance"]


def test_every_failing_check_reports_in_one_round_in_the_reports_order() -> None:
    results = _judge(
        disk_fraction=0.95,
        gateway=GatewayReading(False, True),
        population=_population(PopulationRead.DB_UNREACHABLE, 0, NeverLaunched()),
        schema_skew=True,
        service_failures=("frontend",),
        walg_enabled=True,
        walg_failure="WAL archiving: the archiver is failing",
        editable_violations=("x",),
        source_tree=SourceTreeReading(False, ("tracked change: M a.py",)),
        provider=(
            _provider("provider_balance", "FAIL: provider balance — low"),
            _provider("provider_blocked_agents", "FAIL: provider blocked agents — halted"),
        ),
    )

    assert list(_failures(results)) == [
        "disk_usage",
        "gateway_liveness",
        "agent_population",
        "schema",
        "service_probe",
        "walg_archive",
        "editable_install",
        "source_tree",
        "provider_balance",
        "provider_blocked_agents",
    ]


def test_a_failed_round_spends_three_attempts_and_two_waits() -> None:
    # meiva:note
    # A brief gateway or data-plane restart window is filtered before an outage is declared;
    # the budget is spent exactly once per round (task #5064).
    assert liveness_waits(3, 30.0) == (30.0, 30.0)


def test_one_attempt_waits_for_nothing_and_none_is_refused() -> None:
    assert liveness_waits(1, 30.0) == ()
    with pytest.raises(ValueError, match="at least one attempt"):
        liveness_waits(0, 30.0)
