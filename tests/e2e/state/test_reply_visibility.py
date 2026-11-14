from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

from base.agents.messages.reply_intent import REPLY_VISIBILITY_CORRECTION
from base.sessions.tests.poll_until import poll_until
from tests.e2e._credentials import gateway_get
from tests.e2e._db import chat_and_wait
from tests.e2e.fakes.scenario_recording import model_inputs
from tests.e2e.fakes.scenarios.message_flow import (
    HIDDEN_REPLY,
    MARKED_REPLY,
    UNMARKED_REPLY,
)
from tests.e2e.fixture_environment import E2EEnv


def _chat_items(gateway_url: str, agent_id: int, wanted: tuple[str, ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def replies_projected() -> tuple[bool, object]:
        nonlocal items
        items = gateway_get(
            f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        payloads = [str(it["payload"]) for it in items if it["kind"] == "agent_chat"]
        return all(payload in payloads for payload in wanted), {"payloads": payloads}

    poll_until(
        replies_projected,
        timeout=90.0,
        interval=0.5,
        what=f"the scripted replies reach agent {agent_id} timeline",
    )
    return [it for it in items if it["kind"] == "agent_chat"]


# meiva:note
# The product rule has two halves and this test owns both: text without a visibility line is
# folded into Details (never promoted to the main conversation), and the model is asked once to
# correct it. Every other e2e scenario scripts a compliant model; this one is the negative case
# that keeps the folding behaviour inside the tested surface.
# meiva:link base/agents/messages/docs/metadata-contracts.md
@pytest.mark.scenario("tests.e2e.fakes.scenarios.message_flow:build_visibility_correction")
def test_unmarked_text_folds_into_details_and_the_correction_asks_once(
    e2e_env: E2EEnv,
) -> None:
    agent_id = e2e_env.agent_id
    chat_and_wait(agent_id, "answer me")

    items = _chat_items(e2e_env.gateway_url, agent_id, (UNMARKED_REPLY, MARKED_REPLY))
    by_payload = {str(it["payload"]): it for it in items}
    assert by_payload[UNMARKED_REPLY]["user_facing"] is False, by_payload[UNMARKED_REPLY]
    assert by_payload[MARKED_REPLY]["user_facing"] is True, by_payload[MARKED_REPLY]

    inputs = model_inputs(agent_id)
    assert len(inputs) == 2, f"expected the answer and its correction, saw {len(inputs)} calls"
    # meiva:note
    # The note's text is the product's own `REPLY_VISIBILITY_CORRECTION` inside the system-note
    # wrapper, read from its owner rather than copied: rewording it must not silently weaken this.
    second_call = [str(message.get("text", "")) for message in inputs[1]]
    assert any(REPLY_VISIBILITY_CORRECTION in text for text in second_call), second_call

    # meiva:note
    # The other half of the rule: a reply the model marks hidden is projected into Details, so the
    # two halves are held by the same test -- no marker may promote text, and a hidden marker may
    # never leave text in the main conversation.
    chat_and_wait(agent_id, "now hide it")
    hidden = {
        str(it["payload"]): it for it in _chat_items(e2e_env.gateway_url, agent_id, (HIDDEN_REPLY,))
    }
    assert hidden[HIDDEN_REPLY]["user_facing"] is False, hidden[HIDDEN_REPLY]


@pytest.mark.scenario("tests.e2e.fakes.scenarios.message_flow:build_visibility_correction")
def test_native_inbound_body_survives_live_view_and_cold_reload(e2e_env: E2EEnv) -> None:
    body = "First paragraph preserves a literal [2026-11-14 Sat 16:05:51].\n\nSecond paragraph remains here."
    page = e2e_env.page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    chat_and_wait(e2e_env.agent_id, body)
    items = gateway_get(
        f"{e2e_env.gateway_url}/api/agents/{e2e_env.agent_id}/timeline?limit=100"
    ).json()["items"]
    assert any(item["kind"] == "inbound_chat" and item["payload"] == body for item in items)
    for reload in (False, True):
        if reload:
            page.reload()
        row = page.locator('[data-message-role="user"]').filter(
            has_text="First paragraph preserves"
        )
        expect(row.get_by_test_id("user-message-bubble")).to_contain_text(
            "Second paragraph remains here."
        )
        row.hover()
        row.get_by_role("button", name="Copy message").click()
        expect(row.get_by_role("button", name="Copied", exact=True)).to_be_visible()
        assert page.evaluate("navigator.clipboard.readText()") == body
