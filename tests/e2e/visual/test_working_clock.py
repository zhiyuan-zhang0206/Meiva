from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual.stubbed_home import (
    OVERRIDE_BASE_URL,
    new_context,
    open_page,
    timeline_snapshot,
)

WORKING_ROW = '[data-testid="pending-live-activity-window"]'
TURN_TITLE = '[data-testid="turn-toggle"]'
WORKING_TEXT = re.compile(r"^Working for (?=\d)(?:(\d+)m)? ?(?:(\d+)s)?$")
SECOND_TICKS_MS = 3_300

_ARRIVED = {
    "role": "inbound_arrived",
    "agent_id": 1,
    "inbound_id": 7,
    "kind": "chat",
    "source": "user",
    "content": "Fresh prompt",
}


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


def _iso(ago_seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=ago_seconds)).isoformat().replace("+00:00", "Z")


def _item(
    item_id: str, kind: str, source: str | None, created_at: str | None, **extra: object
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "kind": kind,
        "payload": f"{kind} {item_id}",
        "show_timestamp": True,
        "source": source,
        "created_at": created_at,
        "inbound_id": None,
        **extra,
    }


def _native_items() -> list[dict[str, object]]:
    return [
        _item("1.1", "inbound_chat", "user", _iso(3600), inbound_id=1),
        _item("1.2", "agent_chat", "agent", _iso(3600), user_facing=False),
        _item("1.3", "inbound_chat", "agent:228", _iso(20), inbound_id=7),
        _item("1.4", "agent_code", "agent", _iso(18), activity_pending=True),
    ]


def _external_items() -> list[dict[str, object]]:
    return [
        _item("h10.0", "inbound_chat", "user", _iso(3600), inbound_id=1),
        _item("h20.0", "agent_code", "agent", _iso(3600)),
        _item("h20.1", "agent_chat", "agent", _iso(3600), user_facing=False),
        _item("h30.0", "inbound_chat", "agent:228", _iso(30), inbound_id=7),
        _item("h40.0", "agent_code", "agent", _iso(28), activity_pending=True),
    ]


def _open_active(browser: Browser, base_url: str, items: Sequence[Mapping[str, object]]) -> Page:
    page = open_page(new_context(browser, 1280), base_url, "/", stub_api=True, scripted_sse=True)
    page.wait_for_function("() => window.__meivaSse && window.__meivaSse.ready()", timeout=15_000)
    delivered: int = page.evaluate(
        "events => window.__meivaSse.emit(events)", [_ARRIVED, timeline_snapshot(items)]
    )
    assert delivered == 2, f"the stream took {delivered} of 2 events"
    return page


def _seconds(text: str) -> int:
    match = WORKING_TEXT.match(text.strip())
    assert match, f"not a counting Working title: {text!r}"
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


@pytest.mark.parametrize(
    "items",
    [_native_items, _external_items],
    ids=["native-earlier-one-item-turn", "external-harness-no-durations"],
)
def test_the_work_block_header_counts_seconds_while_active(
    playwright_browser: Browser,
    frontend_target: str,
    items: Callable[[], list[dict[str, object]]],
) -> None:
    page = _open_active(playwright_browser, frontend_target, items())
    try:
        title = page.locator(TURN_TITLE).last
        expect(title).to_have_text(WORKING_TEXT, timeout=5_000)
        first = _seconds(title.inner_text())
        page.wait_for_timeout(SECOND_TICKS_MS)
        second = _seconds(title.inner_text())
        assert second >= first + 2, f"the header stopped counting: {first}s -> {second}s"
        assert second < 600, f"the header absorbed the idle gap: {second}s"
    finally:
        page.context.close()


def test_the_pending_row_counts_seconds_when_the_prompt_has_no_timestamp(
    playwright_browser: Browser, frontend_target: str
) -> None:
    prompt = _item("1.1", "inbound_chat", "user", None, inbound_id=7)
    page = _open_active(playwright_browser, frontend_target, [prompt])
    try:
        row = page.locator(WORKING_ROW)
        expect(row).to_have_text(WORKING_TEXT, timeout=5_000)
        first = _seconds(row.inner_text())
        page.wait_for_timeout(SECOND_TICKS_MS)
        second = _seconds(row.inner_text())
        assert second >= first + 2, f"the pending row stopped counting: {first}s -> {second}s"
    finally:
        page.context.close()
