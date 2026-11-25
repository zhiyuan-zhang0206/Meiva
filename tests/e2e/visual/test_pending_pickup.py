from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual.stubbed_home import (
    OVERRIDE_BASE_URL,
    new_context,
    open_page,
    timeline_snapshot,
)

WORKING_ROW = '[data-testid="pending-live-activity-window"]'
TURN_TITLE = '[data-testid="turn-toggle"]'

_EARLIER_PROMPT = {
    "item_id": "1.1",
    "kind": "inbound_chat",
    "payload": "Earlier prompt",
    "show_timestamp": True,
    "source": "user",
    "created_at": "2026-08-07T00:00:00Z",
    "inbound_id": 1,
}
_EARLIER_WORK = {
    "item_id": "1.2",
    "kind": "agent_reasoning",
    "payload": "Earlier work",
    "show_timestamp": True,
    "source": "agent",
    "created_at": "2026-08-07T00:00:05Z",
    "reasoning_ms": 2000,
}
_QUEUED_PROMPT = {
    "item_id": "1.3",
    "kind": "inbound_chat",
    "payload": "Fresh prompt",
    "show_timestamp": True,
    "source": "user",
    "created_at": "2026-08-07T00:01:00Z",
    "inbound_id": 7,
}
_ARRIVED = {
    "role": "inbound_arrived",
    "agent_id": 1,
    "inbound_id": 7,
    "kind": "chat",
    "source": "user",
    "content": "Fresh prompt",
}
_PENDING_ROW = {
    "id": 7,
    "source": "user",
    "sender": "user",
    "content": "Fresh prompt",
    "images": None,
    "created_at": "2026-08-07T00:01:00Z",
}


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


def _emit(page: Page, *events: Mapping[str, object]) -> None:
    delivered = page.evaluate("events => window.__meivaSse.emit(events)", list(events))
    assert delivered == len(events), f"the stream took {delivered} of {len(events)} events"


def _open_with(browser: Browser, base_url: str, items: Sequence[Mapping[str, object]]) -> Page:
    ctx = new_context(browser, 1280)
    page = open_page(ctx, base_url, "/", stub_api=True, scripted_sse=True)

    def pending(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps([_PENDING_ROW]))

    page.route("**/api/agents/1/pending", pending)
    page.wait_for_function("() => window.__meivaSse && window.__meivaSse.ready()", timeout=15_000)
    _emit(page, timeline_snapshot(items))
    return page


@pytest.mark.parametrize(
    "earlier",
    [[], [_EARLIER_PROMPT], [_EARLIER_PROMPT, _EARLIER_WORK]],
    ids=["empty-timeline", "stale-last-prompt", "earlier-work-block"],
)
def test_a_queued_message_presents_no_work_until_it_is_on_the_timeline(
    playwright_browser: Browser, frontend_target: str, earlier: list[dict[str, object]]
) -> None:
    page = _open_with(playwright_browser, frontend_target, earlier)
    try:
        _emit(page, _ARRIVED)
        expect(page.locator('[data-testid="pending-strip"]')).to_be_visible(timeout=5_000)
        expect(page.locator(WORKING_ROW)).to_have_count(0)
        for title in page.locator(TURN_TITLE).all_inner_texts():
            assert "Working" not in title, title
        _emit(page, timeline_snapshot([*earlier, _QUEUED_PROMPT]))
        expect(page.locator(WORKING_ROW)).to_have_count(1, timeout=5_000)
    finally:
        page.context.close()
