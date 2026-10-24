# meiva:note
# What a start reports and exits with after the readiness wait.

from __future__ import annotations

import pytest

from base.host.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from cli.commands.lifecycle.start.readiness_plan import (
    ReadinessFacts,
    ReadinessVerdict,
    archive_assertion_needed,
    critical_launch_failures,
    readiness_verdict,
    shows_gateway_hint,
)

_CRITICAL = frozenset({"meiva-gateway", "meiva-frontend"})


def _facts(
    *,
    failed: tuple[str, ...] = (),
    unready: bool = False,
    non_critical: bool = False,
) -> ReadinessFacts:
    return ReadinessFacts(
        launch_failed=failed,
        critical=_CRITICAL,
        unready=unready,
        non_critical_unready=non_critical,
    )


def test_a_ready_start_reports_nothing_and_carries_on() -> None:
    assert readiness_verdict(_facts()) == ReadinessVerdict(
        launch_failure_line=None,
        critical_launch_failures=(),
        report_unready=False,
        report_non_critical_unready=False,
        exit_code=None,
    )


def test_an_unready_core_service_fails_the_start() -> None:
    verdict = readiness_verdict(_facts(unready=True))

    assert verdict.exit_code == SERVICES_NOT_READY_EXIT_CODE
    assert verdict.report_unready is True


def test_an_unready_optional_service_is_reported_and_allows_the_start() -> None:
    verdict = readiness_verdict(_facts(non_critical=True))

    assert verdict.exit_code is None
    assert verdict.report_non_critical_unready is True
    assert verdict.report_unready is False


def test_a_core_service_that_could_not_be_launched_fails_the_start() -> None:
    verdict = readiness_verdict(_facts(failed=("meiva-gateway",)))

    assert verdict.exit_code == SERVICES_NOT_READY_EXIT_CODE
    assert verdict.critical_launch_failures == ("meiva-gateway",)


def test_an_optional_service_that_could_not_be_launched_is_named_but_does_not_fail() -> None:
    verdict = readiness_verdict(_facts(failed=("meiva-browser-mcp",)))

    assert verdict.exit_code is None
    assert verdict.critical_launch_failures == ()
    assert verdict.launch_failure_line == (
        "\n✗ 1 service(s) could not be launched : meiva-browser-mcp"
    )


def test_every_failure_is_named_in_one_line_in_launch_order() -> None:
    verdict = readiness_verdict(_facts(failed=("meiva-browser-mcp", "meiva-frontend")))

    assert verdict.launch_failure_line == (
        "\n✗ 2 service(s) could not be launched : meiva-browser-mcp, meiva-frontend"
    )
    assert verdict.critical_launch_failures == ("meiva-frontend",)


def test_critical_failures_keep_only_the_core_names() -> None:
    failed = ("meiva-labeler", "meiva-gateway", "meiva-watcher", "meiva-frontend")

    assert critical_launch_failures(failed, _CRITICAL) == ("meiva-gateway", "meiva-frontend")


def test_a_start_with_no_failures_records_no_critical_failure() -> None:
    assert critical_launch_failures((), _CRITICAL) == ()


@pytest.mark.parametrize(
    ("roles", "gateway_unready", "asserts"),
    [
        ({"gateway"}, False, True),
        ({"gateway", "agent-runner"}, False, True),
        ({"gateway"}, True, False),
        ({"agent-runner"}, False, False),
    ],
    ids=["gateway", "both roles", "gateway unready", "runner"],
)
def test_the_archive_is_asserted_where_the_gateways_postgres_serves(
    roles: set[str], gateway_unready: bool, asserts: bool
) -> None:
    assert archive_assertion_needed(roles, gateway_unready=gateway_unready) is asserts


@pytest.mark.parametrize(
    ("launched", "unready", "shown"),
    [
        ({"gateway", "frontend"}, False, True),
        ({"gateway"}, True, False),
        ({"frontend"}, False, False),
    ],
    ids=["ready gateway", "unready", "no gateway launched"],
)
def test_the_gateway_hint_is_for_a_ready_gateway_this_start_launched(
    launched: set[str], unready: bool, shown: bool
) -> None:
    assert shows_gateway_hint(launched, unready=unready) is shown
