# meiva:note
# The texts a relay pushes, as values: the ACK command, the envelope for one batch and the
# start hint. The envelope is the only thing the external session reads, so what it must
# carry is asserted here.

from __future__ import annotations

import shlex

import pytest

from cli.commands.agents.relay.inbox import DeliveryBudget, LeaseRef
from cli.commands.agents.relay.push_text import ack_command, activation_hint, message_push
from cli.commands.agents.tests.relay.relay_inputs import LEASE_UUID, REF, message

BUDGET = DeliveryBudget(ack_window_seconds=180, max_delivery_attempts=2)


def test_a_session_number_scopes_the_ack_command_to_its_agent() -> None:
    assert ack_command(5, [11, 13], agent_id=42) == "meiva impersonate ack 5 11 13 --agent 42"


def test_a_lease_uuid_needs_no_agent_scope() -> None:
    assert ack_command(LEASE_UUID, [1]) == f"meiva impersonate ack {LEASE_UUID} 1"


def test_the_ack_command_is_one_shell_safe_line() -> None:
    words = shlex.split(ack_command(5, [1, 2], agent_id=42))
    assert words == ["meiva", "impersonate", "ack", "5", "1", "2", "--agent", "42"]


def test_the_envelope_carries_the_ids_the_bodies_and_the_ack_command() -> None:
    push = message_push(
        REF,
        [message(5), message(6, kind="chat", source="user")],
        budget=BUDGET,
        redelivery=False,
        max_chars=None,
    )
    assert push.startswith("Meiva message agent=42 lease=5 ids=5,6")
    assert "[id=5] kind=system_note from=system" in push
    assert "[id=6] kind=chat from=user" in push
    assert "body 5" in push and "body 6" in push
    assert "Delivery attempt 1/2" in push
    assert "ACK after receiving this message: meiva impersonate ack 5 5 6 --agent 42" in push
    assert "ACK within 180s. At most 2 total attempts per message;" in push
    assert "re-delivery" not in push


def test_a_redelivery_says_so_and_counts_the_attempt() -> None:
    push = message_push(
        REF, [message(11, attempts=1)], budget=BUDGET, redelivery=True, max_chars=None
    )
    assert "ids=11 (re-delivery: unacknowledged)" in push
    assert "Delivery attempt 2/2" in push


def test_a_batch_with_a_final_delivery_warns_that_a_missed_ack_pauses_it() -> None:
    push = message_push(
        REF, [message(11, attempts=1)], budget=BUDGET, redelivery=True, max_chars=None
    )
    assert "This batch includes a final delivery" in push
    assert "At most" not in push


def test_the_budget_a_lease_snapshotted_is_the_one_the_envelope_states() -> None:
    push = message_push(
        REF, [message(1)], budget=DeliveryBudget(7, 1), redelivery=False, max_chars=None
    )
    assert "ACK within 7s." in push
    assert "Delivery attempt 1/1" in push
    assert "final delivery" in push


def test_claude_envelope_truncates_long_content_but_keeps_the_ack_line() -> None:
    push = message_push(
        REF, [message(1, content="x" * 5000)], budget=BUDGET, redelivery=False, max_chars=2000
    )
    assert "truncated — run the inbox command to read the full body before acknowledging" in push
    assert "x" * 2001 not in push
    assert "ACK after receiving this message:" in push


def test_a_body_within_the_budget_is_untouched() -> None:
    push = message_push(
        REF, [message(1, content="y" * 2000)], budget=BUDGET, redelivery=False, max_chars=2000
    )
    assert "truncated" not in push


def test_the_activation_hint_states_the_push_contract() -> None:
    hint = activation_hint(REF, BUDGET)
    assert hint.startswith("Meiva control active: agent=42 lease=5.")
    assert "meiva impersonate ack 5 --agent 42 ID..." in hint
    assert "meiva impersonate inbox 5 --agent 42" in hint
    assert "Each message has 2 delivery attempts, with 180s to ACK each" in hint
    assert "after processing" not in hint


@pytest.mark.parametrize("ref", [LeaseRef(42, LEASE_UUID)])
def test_a_lease_uuid_hint_carries_no_agent_scope(ref: LeaseRef) -> None:
    hint = activation_hint(ref, BUDGET)
    assert f"meiva impersonate ack {LEASE_UUID} ID..." in hint
    assert "--agent" not in hint
