from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from playwright.sync_api import Page, Response, expect

from base.api_contracts.agent_contract import AgentStatus
from base.sessions.tests.poll_until import poll_until
from tests.e2e._credentials import gateway_get, gateway_post
from tests.e2e._db import chat_and_wait, wait_for_status
from tests.e2e.fakes.scenarios.message_flow import READING_REPLIES
from tests.e2e.fixture_environment import E2EEnv
from tests.fixtures.gateway_config import post_spawn

_STORAGE_KEY = "timeline-bookmarks:v1"
_OFFSET_TOLERANCE_PX = 1.0
_BOOKMARK_JS = """({key, agent}) => {
  const value = JSON.parse(sessionStorage.getItem(key) ?? 'null');
  return value?.version === 1 ? value.entries.find(entry => entry.agentId === agent) ?? null : null;
}"""
_OFFSET_JS = """id => {
  const viewport = document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]');
  const node = viewport?.querySelector(`.timeline-item[data-item-id="${CSS.escape(id)}"]`);
  if (!node) throw new Error('canonical reading node is not materialized');
  return node.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
}"""


def _source_item(env: E2EEnv, number: int) -> dict[str, Any]:
    selected: dict[str, Any] = {}

    def committed() -> tuple[bool, object]:
        nonlocal selected
        response = gateway_get(f"{env.gateway_url}/api/agents/{env.agent_id}/timeline?limit=1000")
        response.raise_for_status()
        matches = [
            item
            for item in response.json()["items"]
            if item["kind"] == "agent_chat" and item["payload"] == READING_REPLIES[number - 1]
        ]
        if len(matches) == 1:
            selected = matches[0]
        return bool(selected.get("reading_locator") and selected.get("source_message_id")), {
            "matches": len(matches),
            "item_id": selected.get("item_id"),
        }

    poll_until(
        committed,
        timeout=45.0,
        interval=0.3,
        what="the native source commits a stable reading member",
    )
    return selected


def _read_inside_reply(page: Page, agent: int, item: dict[str, Any]) -> dict[str, Any]:
    viewport = page.locator('[data-slot="scroll-area-viewport"]').filter(
        has=page.get_by_role("log")
    )
    viewport.hover()
    page.mouse.wheel(0, 100_000)
    page.locator(f'.timeline-item[data-item-id="{item["item_id"]}"]').wait_for(
        state="attached", timeout=15_000
    )
    page.evaluate(
        """id => {
      const viewport = document.querySelector('[role=log]').closest('[data-slot=scroll-area-viewport]');
      const node = viewport.querySelector(`.timeline-item[data-item-id="${CSS.escape(id)}"]`);
      viewport.dispatchEvent(new WheelEvent('wheel', {deltaY: -1, bubbles: true}));
      viewport.scrollTop += node.getBoundingClientRect().top - viewport.getBoundingClientRect().top + 100;
      viewport.dispatchEvent(new Event('scroll'));
    }""",
        item["item_id"],
    )
    page.wait_for_function(
        """({key, agent, id, locator}) => {
      const value = JSON.parse(sessionStorage.getItem(key) ?? 'null');
      const entry = value?.version === 1 ? value.entries.find(row => row.agentId === agent) : null;
      return entry?.locator === locator && entry.position.anchor.itemId === id;
    }""",
        arg={
            "key": _STORAGE_KEY,
            "agent": agent,
            "id": item["item_id"],
            "locator": item["reading_locator"],
        },
        timeout=15_000,
    )
    saved = cast(dict[str, Any], page.evaluate(_BOOKMARK_JS, {"key": _STORAGE_KEY, "agent": agent}))
    actual = float(page.evaluate(_OFFSET_JS, item["item_id"]))
    error = abs(actual - float(saved["position"]["anchor"]["viewportTop"]))
    assert error <= _OFFSET_TOLERANCE_PX, {
        "actual_offset": actual,
        "saved": saved,
        "error_css_px": error,
    }
    return saved


def _assert_restored(
    page: Page,
    env: E2EEnv,
    item: dict[str, Any],
    saved: dict[str, Any],
    action: Callable[[], object],
) -> float:
    def selected_window(response: Response) -> bool:
        url = urlsplit(response.url)
        return (
            response.request.method == "GET"
            and url.path == f"/api/agents/{env.agent_id}/timeline/window"
            and parse_qs(url.query).get("anchor") == [saved["locator"]]
        )

    with page.expect_response(selected_window, timeout=15_000) as read:
        action()
    response = read.value
    assert response.ok, (response.status, response.text())
    source = response.json()
    members = [row for row in source["items"] if row["item_id"] == source["anchor_item_id"]]
    assert source["reading_source"] and len(members) == 1, source
    target = members[0]
    assert target["item_id"] == item["item_id"], target
    assert target["source_message_id"] == item["source_message_id"], target
    assert target["source_block_idx"] == item["source_block_idx"], target
    assert target["reading_locator"] == saved["locator"], target
    expect(page.get_by_test_id("cold-load-ring")).to_have_count(0)
    page.evaluate(
        "() => document.fonts.ready.then(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))"
    )
    actual = float(page.evaluate(_OFFSET_JS, target["item_id"]))
    expected = float(saved["position"]["anchor"]["viewportTop"])
    error = abs(actual - expected)
    assert error <= _OFFSET_TOLERANCE_PX, {
        "item_id": target["item_id"],
        "actual_offset": actual,
        "saved_offset": expected,
        "error_css_px": error,
    }
    return error


@pytest.mark.scenario("tests.e2e.fakes.scenarios.message_flow:build_reading_reload")
def test_same_tab_reload_and_agent_return_preserve_source_position(
    e2e_env: E2EEnv, record_property: Callable[[str, object], None]
) -> None:
    env = e2e_env
    chat_and_wait(env.agent_id, "reading first")
    chat_and_wait(env.agent_id, "reading second")
    item = _source_item(env, 2)
    response = post_spawn(env.gateway_url, {"spawner": "user"}, timeout=30.0)
    response.raise_for_status()
    other = int(response.json()["id"])
    page = env.page
    try:
        page.goto(env.agent_url)
        page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=15_000)
        saved = _read_inside_reply(page, env.agent_id, item)
        page.locator("li.agent-row-item").get_by_role(
            "button", name=re.compile(rf"^#{other}\b")
        ).click()
        page.wait_for_url(re.compile(rf"[?&]agent_id={other}\b"))
        error = _assert_restored(
            page,
            env,
            item,
            saved,
            lambda: (
                page.locator("li.agent-row-item")
                .get_by_role("button", name=re.compile(rf"^#{env.agent_id}\b"))
                .click()
            ),
        )
        record_property("agent_return_error_css_px", error)
        record_property(
            "reload_error_css_px", _assert_restored(page, env, item, saved, page.reload)
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            gateway_post(f"{env.gateway_url}/api/agents/{other}/terminate", timeout=5.0)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.message_flow:build_reading_reload")
def test_latest_live_source_member_is_saved_and_restored_after_reload(
    e2e_env: E2EEnv, record_property: Callable[[str, object], None]
) -> None:
    env = e2e_env
    chat_and_wait(env.agent_id, "reading first")
    chat_and_wait(env.agent_id, "reading second")
    page = env.page
    page.goto(env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=15_000)
    previous = _read_inside_reply(page, env.agent_id, _source_item(env, 2))
    page.fill('[data-testid="composer-input"]', "reading latest")
    page.click('[data-testid="composer-send"]')
    latest = _source_item(env, 3)
    wait_for_status(env.agent_id, AgentStatus.IDLING.value)
    saved = _read_inside_reply(page, env.agent_id, latest)
    assert saved["locator"] != previous["locator"]
    assert saved["position"]["anchor"]["itemId"] == latest["item_id"]
    record_property(
        "latest_reload_error_css_px", _assert_restored(page, env, latest, saved, page.reload)
    )
    assert page.locator("[data-timeline-spacer]").count() > 0, (
        "the native fixture never exercised windowed rendering"
    )
