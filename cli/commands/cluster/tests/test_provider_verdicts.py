# meiva:note
# The provider-account guard's verdicts, decided from readings.
#
# Born of the 2026-09-18 outage: the DeepSeek balance hit zero, every deepseek-model call
# answered HTTP 402 and 23 agents were halted for 5h43m. The balance check fails before the
# account runs dry; the blocked-agents check fails while the fleet is still halted. Both are
# fail-open on read errors, and neither prints the live reading in its failure message (the
# message must stay stable so the alert groups, the reading rides a detail line).

from __future__ import annotations

import pytest

from cli.commands.cluster.plans.provider_verdicts import (
    BALANCE_CHECK,
    BLOCKED_AGENTS_CHECK,
    BalancePayload,
    BalanceUnreadable,
    NoProviderKey,
    judge_balance,
    judge_blocked_agents,
)


def _payload(total: str, *, currency: str = "CNY", available: bool = True) -> BalancePayload:
    return BalancePayload(
        {
            "is_available": available,
            "balance_infos": [{"currency": currency, "total_balance": total}],
        }
    )


def test_a_balance_at_the_minimum_passes_silently() -> None:
    check = judge_balance(_payload("500.00"), 500.0)

    assert check.check == BALANCE_CHECK
    assert check.failure is None and check.notes == ()


def test_a_balance_below_the_minimum_fails_with_a_stable_message() -> None:
    first = judge_balance(_payload("100.00"), 500.0)
    second = judge_balance(_payload("99.50"), 500.0)

    assert first.failure is not None
    assert first.failure == second.failure
    assert "MEIVA_PROVIDER_GUARD_BALANCE_MIN_CNY=¥500" in first.failure
    assert "100.00" not in first.failure and "99.50" not in first.failure
    assert first.notes == ("  (provider balance detail: ¥100.00, available=True, minimum=¥500)",)
    assert "¥99.50" in second.notes[0]


def test_an_account_the_provider_calls_unavailable_fails_even_when_funded() -> None:
    check = judge_balance(_payload("9999.00", available=False), 500.0)

    assert check.failure is not None and "is_available=false" in check.failure
    assert "available=False" in check.notes[0]


def test_a_host_without_a_key_skips_and_says_so() -> None:
    check = judge_balance(NoProviderKey(), 500.0)

    assert check.failure is None
    assert check.notes == (
        "  (provider balance check skipped: DEEPSEEK_API_KEY not configured on this host)",
    )


def test_a_balance_read_error_is_fail_open() -> None:
    check = judge_balance(BalanceUnreadable("HTTP 502"), 500.0)

    assert check.failure is None
    assert check.notes == ("  (provider balance check skipped: HTTP 502)",)


def test_a_payload_without_a_cny_entry_skips_rather_than_guessing_at_fx() -> None:
    check = judge_balance(_payload("100.00", currency="USD"), 500.0)

    assert check.failure is None
    assert "no CNY balance entry" in check.notes[0]


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({}, "response has no balance_infos"),
        ({"balance_infos": "nope"}, "response has no balance_infos"),
        (
            {"balance_infos": [{"currency": "CNY", "total_balance": "n/a"}]},
            "CNY total_balance is not numeric",
        ),
        ({"balance_infos": [{"currency": "CNY"}]}, "CNY total_balance is not numeric"),
        ({"balance_infos": ["junk", {"currency": "USD"}]}, "no CNY balance entry"),
    ],
)
def test_a_payload_that_is_not_shaped_like_a_balance_answer_is_a_read_error(
    body: dict[str, object], why: str
) -> None:
    check = judge_balance(BalancePayload(body), 500.0)

    assert check.failure is None
    assert why in check.notes[0]


def test_blocked_agents_at_the_threshold_fail_with_a_stable_message() -> None:
    first = judge_blocked_agents(3, 3, 24.0)
    second = judge_blocked_agents(7, 3, 24.0)

    assert first.check == BLOCKED_AGENTS_CHECK
    assert first.failure is not None and first.failure == second.failure
    assert "MEIVA_PROVIDER_GUARD_BLOCKED_AGENTS_MIN=3" in first.failure
    assert first.notes[0].startswith("  (provider blocked-agents detail: 3 agent(s) halted")
    assert "within 24h" in first.notes[0]
    assert "7 agent(s)" in second.notes[0]


def test_blocked_agents_below_the_threshold_are_silent() -> None:
    check = judge_blocked_agents(2, 3, 24.0)

    assert check.failure is None and check.notes == ()


def test_an_unreadable_agent_table_is_fail_open_and_says_so() -> None:
    check = judge_blocked_agents(None, 3, 24.0)

    assert check.failure is None
    assert check.notes == ("  (provider blocked-agents check skipped: agent table unreachable)",)
