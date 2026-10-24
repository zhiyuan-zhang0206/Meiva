# meiva:note
# The provider-account guard's readings, against the real things it reads.
#
# The provider's balance endpoint is a scripted listener on a loopback port (or a port nobody
# listens on), the agent table the suite's throwaway Postgres, and the configuration a real
# command configuration owner. What each check says is decided from values in
# `test_provider_verdicts.py`; this file proves the shell reads what the verdicts judge, reads
# nothing for a disabled check, and fails open exactly where the verdicts expect it to.

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from base.agents.recovery.breaker import HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS
from base.db import Database
from base.host.net.tests.local_http import closed_port_url
from cli.commands.cluster._provider_guard import (
    ProviderGuardConfig,
    fetch_balance,
    halted_agents_count,
    provider_guard_checks,
    provider_guard_config,
)
from cli.commands.cluster.plans.provider_verdicts import (
    BALANCE_CHECK,
    BLOCKED_AGENTS_CHECK,
    BalancePayload,
    BalanceUnreadable,
)
from cli.commands.cluster.tests.gateway_server import Reply, serving
from cli.commands.cluster.tests.health_probe_inputs import live_agent
from cli.commands.cluster.tests.health_probe_inputs import (
    unreachable_database as unreachable_database,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime

_BALANCE = ("GET", "/user/balance")
_LOW = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "12.50"}]}


def _guard(**changes: Any) -> ProviderGuardConfig:
    base = ProviderGuardConfig(
        balance_enabled=True,
        deepseek_api_key="sk-test",
        balance_url="http://127.0.0.1:1/user/balance",
        balance_timeout_s=2.0,
        balance_min_cny=500.0,
        blocked_agents_enabled=True,
        blocked_agents_window_hours=24.0,
        blocked_agents_min=1,
    )
    return dataclasses.replace(base, **changes)


def _halt(conn: psycopg.Connection, *, streak: int, hours_ago: float) -> int:
    agent_id = live_agent(conn)
    conn.execute(
        "UPDATE agents_meta SET permanent_reject_streak = %s, "
        "last_turn_fatal_at = now() - make_interval(secs => %s) WHERE id = %s",
        (streak, hours_ago * 3600.0, agent_id),
    )
    conn.commit()
    return agent_id


def test_the_balance_read_presents_the_key_and_returns_the_payload() -> None:
    with serving({_BALANCE: Reply(body=_LOW)}) as provider:
        reading = fetch_balance(f"{provider.url}/user/balance", "sk-test", 2.0)

    assert reading == BalancePayload(_LOW)
    (seen,) = provider.requests
    assert seen.headers["authorization"] == "Bearer sk-test"
    assert seen.headers["accept"] == "application/json"


def test_an_http_failure_is_a_sanitized_read_error() -> None:
    with serving({_BALANCE: Reply(status=500, body="<html>upstream error</html>")}) as provider:
        reading = fetch_balance(f"{provider.url}/user/balance", "sk-test", 2.0)

    assert reading == BalanceUnreadable("HTTP 500")


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (Reply(body=[1, 2]), "response is not a JSON object"),
        (Reply(body=None), "response is not a JSON object"),
        (Reply(raw=b"<html>not json</html>"), "JSONDecodeError"),
    ],
)
def test_an_answer_that_is_not_a_json_object_is_a_read_error_without_the_body(
    reply: Reply, reason: str
) -> None:
    with serving({_BALANCE: reply}) as provider:
        reading = fetch_balance(f"{provider.url}/user/balance", "sk-test", 2.0)

    assert reading == BalanceUnreadable(reason)


def test_an_unreachable_provider_is_a_read_error_naming_only_the_exception_class() -> None:
    reading = fetch_balance(f"{closed_port_url()}/user/balance", "sk-secret-key", 2.0)

    assert reading == BalanceUnreadable("ConnectError")


def test_only_agents_halted_by_permanent_rejections_inside_the_window_are_counted(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    # meiva:note
    # `permanent_reject_streak >= HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS` is the recovery
    # breaker's durable halt flag and `last_turn_fatal_at` bounds it to the active wave.
    _halt(db_conn, streak=HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, hours_ago=1)
    _halt(db_conn, streak=HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS + 3, hours_ago=23)
    _halt(db_conn, streak=HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS - 1, hours_ago=1)
    _halt(db_conn, streak=HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, hours_ago=30)
    live_agent(db_conn)

    assert halted_agents_count(24.0, database_factory=operator_database) == 2
    assert halted_agents_count(48.0, database_factory=operator_database) == 3
    assert halted_agents_count(0.5, database_factory=operator_database) == 0


def test_a_database_that_cannot_be_queried_is_none_never_a_healthy_zero(
    unreachable_database: Callable[[], Database],
) -> None:
    assert halted_agents_count(24.0, database_factory=unreachable_database) is None


def test_the_checks_run_balance_first_and_both_report_independently(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    # meiva:note
    # Balance first (the root cause), blocked agents second; a low balance also reports the
    # halted agents it caused.
    _halt(db_conn, streak=2, hours_ago=1)
    with serving({_BALANCE: Reply(body=_LOW)}) as provider:
        guard = _guard(balance_url=f"{provider.url}/user/balance")
        checks = provider_guard_checks(guard, database_factory=operator_database)

    assert [check.check for check in checks] == [BALANCE_CHECK, BLOCKED_AGENTS_CHECK]
    assert all(check.failure is not None for check in checks)


def test_a_disabled_balance_check_reads_nothing_from_the_provider(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    with serving({_BALANCE: Reply(body=_LOW)}) as provider:
        guard = _guard(balance_enabled=False, balance_url=f"{provider.url}/user/balance")
        checks = provider_guard_checks(guard, database_factory=operator_database)

    assert [check.check for check in checks] == [BLOCKED_AGENTS_CHECK]
    assert provider.requests == []


def test_a_disabled_blocked_agents_check_is_not_judged_even_with_agents_halted(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    _halt(db_conn, streak=2, hours_ago=1)
    with serving({_BALANCE: Reply(body=_LOW)}) as provider:
        guard = _guard(blocked_agents_enabled=False, balance_url=f"{provider.url}/user/balance")
        checks = provider_guard_checks(guard, database_factory=operator_database)

    assert [check.check for check in checks] == [BALANCE_CHECK]


def test_a_host_without_a_key_skips_the_balance_without_dialing_the_provider(
    db_conn: psycopg.Connection, operator_database: Callable[[], Database]
) -> None:
    with serving({_BALANCE: Reply(body=_LOW)}) as provider:
        guard = _guard(deepseek_api_key=None, balance_url=f"{provider.url}/user/balance")
        (balance, _blocked) = provider_guard_checks(guard, database_factory=operator_database)

    assert balance.failure is None
    assert balance.notes == (
        "  (provider balance check skipped: DEEPSEEK_API_KEY not configured on this host)",
    )
    assert provider.requests == []


def test_an_unreachable_agent_table_skips_the_blocked_check_and_keeps_the_balance_verdict(
    unreachable_database: Callable[[], Database],
) -> None:
    with serving({_BALANCE: Reply(body=_LOW)}) as provider:
        guard = _guard(balance_url=f"{provider.url}/user/balance")
        balance, blocked = provider_guard_checks(guard, database_factory=unreachable_database)

    assert balance.failure is not None
    assert blocked.failure is None
    assert blocked.notes == ("  (provider blocked-agents check skipped: agent table unreachable)",)


def test_the_guard_is_configured_by_the_commands_own_configuration(
    command_runtime: CommandRuntime,
) -> None:
    config = command_runtime.config
    config.set_field("provider_guard_balance_min_cny", 123.0)
    config.set_field("provider_guard_blocked_agents_min", 5)
    config.set_field("deepseek_api_key", SecretStr(" sk-live "))

    guard = provider_guard_config(config)

    assert guard.balance_min_cny == 123.0 and guard.blocked_agents_min == 5
    assert guard.deepseek_api_key == "sk-live"
    assert guard.balance_enabled and guard.blocked_agents_enabled
    assert "sk-live" not in repr(guard)


@pytest.mark.parametrize("secret", [None, SecretStr(""), SecretStr("   ")])
def test_an_unset_or_blank_key_is_no_key(
    command_runtime: CommandRuntime, secret: SecretStr | None
) -> None:
    command_runtime.config.set_field("deepseek_api_key", secret)

    assert provider_guard_config(command_runtime.config).deepseek_api_key is None
