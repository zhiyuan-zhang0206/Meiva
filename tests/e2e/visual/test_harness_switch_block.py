from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from playwright.sync_api import Browser, Page

from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual.stubbed_home import OVERRIDE_BASE_URL, new_context, open_page

SWITCH_SOURCE = "harness_switch"


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


def _item(item_id: str, kind: str, payload: str, **extra: object) -> dict[str, object]:
    return {
        "item_id": item_id,
        "kind": kind,
        "payload": payload,
        "source": extra.pop("source", None),
        "show_timestamp": True,
        "created_at": extra.pop("created_at", "2026-11-25T10:00:00Z"),
        "inbound_id": None,
        **extra,
    }


def _switch(seq: int, payload: str, created_at: str = "2026-11-25T10:00:00Z") -> dict[str, object]:
    return _item(
        f"e{seq}.sw", "system_marker", payload, source=SWITCH_SOURCE, created_at=created_at
    )


def _page(items: Sequence[Mapping[str, object]], has_more: bool) -> dict[str, object]:
    return {"items": list(items), "msg_count": 0, "has_more": has_more, "current_epoch": 3}


HEAD_ITEMS = [
    _switch(3, "Switched harness: codex -> meiva (claude-sonnet-5-5)"),
    _item(
        "40.0", "inbound_chat", "Where did we leave the migration?", source="user", inbound_id=40
    ),
    _item("41.0", "agent_reasoning", "Reading the handoff file first.", source="agent"),
    _item("41.1", "agent_chat", "The migration is applied; the backfill is next.", source="agent"),
]

PAGES: dict[str, object] = {
    "": {**_page(HEAD_ITEMS, True), "msg_count": 44},
    "e3.sw": _page(
        [
            _switch(
                2, "Switched harness: claude_code -> codex (gpt-6.2-sol)", "2026-11-25T09:40:00Z"
            ),
            _item(
                "e2.h900.0",
                "inbound_chat",
                "Run the backfill dry run.",
                source="user",
                inbound_id=30,
            ),
            _item(
                "e2.h1800.0",
                "agent_chat",
                "Dry run finished: 120 agents, 0 conflicts.",
                source="agent",
            ),
        ],
        True,
    ),
    "e2.sw": _page(
        [
            _switch(
                1,
                "Switched harness: meiva -> claude_code (claude-sonnet-5-5)",
                "2026-11-25T09:20:00Z",
            ),
        ],
        True,
    ),
    "e1.sw": _page(
        [
            _item(
                "e0.6.0", "inbound_chat", "Plan the schema change.", source="user", inbound_id=10
            ),
            _item(
                "e0.7.0",
                "agent_reasoning",
                "One facts-only table, written in the switch transaction.",
                source="agent",
            ),
            _item("e0.8.0", "agent_chat", "Plan: add one facts-only table.", source="agent"),
        ],
        False,
    ),
}

ORDER = [
    "e0.6.0",
    "e0.7.0",
    "e0.8.0",
    "e1.sw",
    "e2.sw",
    "e2.h900.0",
    "e2.h1800.0",
    "e3.sw",
    "40.0",
    "41.0",
    "41.1",
]

EXPAND_JS = """
() => {
  for (const sel of ['[data-testid="turn-toggle"][data-expanded="false"]']) {
    for (const btn of document.querySelectorAll(sel)) btn.click();
  }
}
"""

ITEM_IDS_JS = "() => [...document.querySelectorAll('.timeline-item[data-item-id]')].map((n) => n.getAttribute('data-item-id'))"


def _open(browser: Browser, base_url: str) -> Page:
    page = open_page(
        new_context(browser, 1280),
        base_url,
        "/",
        stub_api=True,
        timeline_items=HEAD_ITEMS,
        timeline_pages=PAGES,
    )
    page.wait_for_selector(".timeline-item[data-item-id='41.1']", timeout=15_000)
    return page


def _page_all_history_in(page: Page) -> list[str]:
    ids: list[str] = []
    for _ in range(12):
        page.evaluate(EXPAND_JS)
        page.wait_for_timeout(300)
        page.mouse.move(640, 300)
        page.mouse.wheel(0, -4000)
        page.wait_for_timeout(500)
        ids = page.evaluate(ITEM_IDS_JS)
        if "e0.6.0" in ids:
            break
    page.evaluate(EXPAND_JS)
    page.wait_for_timeout(300)
    return page.evaluate(ITEM_IDS_JS)


def test_epoch_history_pages_in_order_with_switch_blocks_as_work_rows(
    playwright_browser: Browser, frontend_target: str
) -> None:
    page = _open(playwright_browser, frontend_target)
    try:
        ids = _page_all_history_in(page)
        assert [i for i in ids if i in ORDER] == ORDER
        assert page.locator("[data-testid='marker-unrecognized']").count() == 0
        for seq in (1, 2, 3):
            row = page.locator(f".timeline-item[data-item-id='e{seq}.sw']")
            assert row.count() == 1
            assert row.locator("xpath=ancestor::*[@data-turn-id]").count() >= 1
        assert "Harness switch" in page.locator(".timeline-item[data-item-id='e2.sw']").inner_text()
    finally:
        page.context.close()
