# meiva:note
# What retiring the standalone bridge decides, and how the relay's status reads, as values.

from __future__ import annotations

from cli.commands.converge.plans.bridge_retirement import (
    Expired,
    Leave,
    Retire,
    Retry,
    bootout_wait_step,
    plan_retirement,
    still_loaded_message,
)
from cli.commands.converge.plans.bridge_status import (
    RedisBridgeStatus,
    failure_detail,
    not_required,
    ping_status,
    status_line,
    unconfigured,
)


def test_nothing_left_of_the_standalone_bridge_is_left_alone() -> None:
    assert plan_retirement(loaded=False, plist_present=False, installed_present=False) == Leave()


def test_a_loaded_job_is_unloaded_before_its_files_go() -> None:
    assert plan_retirement(loaded=True, plist_present=True, installed_present=True) == Retire(
        unload=True
    )
    assert plan_retirement(loaded=True, plist_present=False, installed_present=False) == Retire(
        unload=True
    )


def test_files_without_a_loaded_job_are_only_removed() -> None:
    assert plan_retirement(loaded=False, plist_present=True, installed_present=False) == Retire(
        unload=False
    )
    assert plan_retirement(loaded=False, plist_present=False, installed_present=True) == Retire(
        unload=False
    )


def test_the_unload_wait_retries_until_the_budget_is_spent() -> None:
    assert bootout_wait_step(elapsed_s=0.0, timeout_s=10.0, interval_s=0.25) == Retry(0.25)
    assert bootout_wait_step(elapsed_s=9.9, timeout_s=10.0, interval_s=0.25) == Retry(0.25)
    assert bootout_wait_step(elapsed_s=10.0, timeout_s=10.0, interval_s=0.25) == Expired()


def test_a_job_that_will_not_unload_keeps_its_files_for_inspection() -> None:
    message = still_loaded_message(
        label="com.meiva.redis-bridge", timeout_s=10.0, plist_path="/u/Library/x.plist"
    )
    assert message == (
        "Redis bridge LaunchAgent com.meiva.redis-bridge remained loaded after 10s; "
        "retained /u/Library/x.plist for inspection"
    )


def test_a_host_without_a_relay_reports_it_serving_nothing_required() -> None:
    assert not_required() == RedisBridgeStatus(
        required=False, endpoint="", serving=True, detail="not required"
    )
    assert status_line(not_required()) == "  · not required (Redis has no local off-box ingress)"


def test_a_ping_that_worked_is_serving_and_one_that_failed_says_why() -> None:
    assert ping_status("10.64.0.7:16380", None) == RedisBridgeStatus(
        required=True, endpoint="10.64.0.7:16380", serving=True, detail="Redis PING succeeded"
    )
    failed = ping_status("10.64.0.7:16380", "connection refused")
    assert not failed.serving and failed.detail == "connection refused"
    assert status_line(failed) == "  ✗ 10.64.0.7:16380 Redis PING: connection refused"


def test_a_serving_relay_prints_a_check_mark() -> None:
    assert (
        status_line(ping_status("10.64.0.7:16380", None))
        == "  ✓ 10.64.0.7:16380 Redis PING: Redis PING succeeded"
    )


def test_an_unconfigured_relay_names_no_endpoint() -> None:
    status = unconfigured("bridge unconfigured: no registry record for /h")
    assert status.required and not status.serving and status.endpoint == ""
    assert status_line(status) == "  ✗ Redis PING: bridge unconfigured: no registry record for /h"


def test_a_client_error_reads_as_its_last_non_empty_line() -> None:
    assert failure_detail("first\n  \n  second  \n", "ConnectionError") == "second"
    assert failure_detail("", "TimeoutError") == "TimeoutError"
