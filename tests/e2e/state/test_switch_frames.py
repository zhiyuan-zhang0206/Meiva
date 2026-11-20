# meiva:note
# Every painted frame of a switch back and of a reload shows the source or the destination. A frame
# probe runs after the app's own layout passes and right before paint: each frame it records whether
# the conversation's rows are visible and, if so, the first readable row and its offset. A return to
# a conversation must never paint its rows anywhere but the saved place, including while the
# authority read is still in flight, and a reload must open on that place.
# meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

from __future__ import annotations

import contextlib
import re
import time
from typing import TypedDict, cast

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e._credentials import gateway_post
from tests.e2e.fixture_environment import E2EEnv
from tests.fixtures.gateway_config import post_spawn

# meiva:note
# A ResizeObserver created inside each frame's requestAnimationFrame is the newest observer, so its
# callback runs after the app's observers (which place the rows) and before the frame is painted.
_FRAME_PROBE = """
(() => {
  window.__frames = [];
  const read = () => {
    const log = document.querySelector('[role=log]');
    const viewport = log && log.closest('[data-slot=scroll-area-viewport]');
    if (!viewport) return { rows: false };
    const column = viewport.querySelector('[data-timeline-column]');
    const hidden = column ? getComputedStyle(column).visibility === 'hidden' : false;
    const box = viewport.getBoundingClientRect();
    for (const row of viewport.querySelectorAll('.timeline-item[data-item-id]')) {
      const rect = row.getBoundingClientRect();
      if (rect.bottom > box.top + 44 && rect.top < box.bottom) {
        return { rows: true, hidden, id: row.dataset.itemId, offset: rect.top - box.top };
      }
    }
    return { rows: false, hidden };
  };
  const probe = document.createElement('div');
  probe.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;pointer-events:none;opacity:0';
  let observer = null;
  const tick = () => {
    if (observer) observer.disconnect();
    if (!probe.isConnected && document.body) document.body.appendChild(probe);
    observer = new ResizeObserver(() => window.__frames.push(read()));
    observer.observe(probe);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})();
"""

_FIRST_ROW = """v => {
  const box = v.getBoundingClientRect();
  for (const row of v.querySelectorAll('.timeline-item[data-item-id]')) {
    const rect = row.getBoundingClientRect();
    if (rect.bottom > box.top + 44 && rect.top < box.bottom) return { id: row.dataset.itemId, offset: rect.top - box.top };
  }
  return null;
}"""

_AUTHORITY_DELAY_S = 1.2


class Frame(TypedDict, total=False):
    rows: bool
    hidden: bool
    id: str
    offset: float


class Place(TypedDict):
    id: str
    offset: float


def _sidebar_row(page: Page, agent_id: int):
    return page.locator("li.agent-row-item").get_by_role(
        "button", name=re.compile(rf"^#{agent_id}\b")
    )


def _assert_destination(frames: list[Frame], ids: set[str], saved: Place, what: str) -> int:
    landed = 0
    for index, frame in enumerate(frames):
        row = frame.get("id")
        offset = frame.get("offset", 0.0)
        if not frame.get("rows") or frame.get("hidden") or row is None or row not in ids:
            continue
        assert row == saved["id"] and abs(offset - saved["offset"]) <= 1, (
            f"{what}: frame {index} painted {row}@{offset:.1f}, "
            f"expected {saved['id']}@{saved['offset']:.1f}"
        )
        landed += 1
    return landed


def _stub_conversation(page: Page, agent: int) -> set[str]:
    messages = [
        {
            "item_id": f"{index}.0",
            "kind": "agent_chat",
            "payload": f"Reply {index}\n\n"
            + "\n\n".join(f"Line {line} of reply {index}." for line in range(4)),
            "created_at": "2026-11-10T00:00:00Z",
            "show_timestamp": False,
            "source": None,
            "inbound_id": None,
        }
        for index in range(101, 141)
    ]
    tail = {"items": messages, "msg_count": len(messages), "has_more": False}
    feed: dict[str, object] = {"mode": "prefix", "seen_id": 0, "items": []}

    def slow_snapshot(route: Route) -> None:
        # meiva:note
        # The authority read stays in flight long enough to tell an immediate return from a waiting one.
        time.sleep(_AUTHORITY_DELAY_S)
        response = route.fetch()
        body = cast(dict[str, object], response.json())
        body["timeline"] = tail
        route.fulfill(response=response, json=body)

    _ = page.route(
        re.compile(rf"/api/agents/{agent}/timeline(?:\?|$)"), lambda route: route.fulfill(json=tail)
    )
    _ = page.route(f"**/api/agents/{agent}/conversation-snapshot", slow_snapshot)
    _ = page.route(
        re.compile(rf"/api/agents/{agent}/user-messages\?"), lambda route: route.fulfill(json=feed)
    )
    return {str(message["item_id"]) for message in messages}


def _frames(page: Page) -> list[Frame]:
    return cast(list[Frame], page.evaluate("() => window.__frames"))


@pytest.mark.scenario("tests.e2e.fakes.scenarios.message_flow:build")
def test_switch_back_and_reload_paint_only_the_saved_place(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent = e2e_env.agent_id
    gateway_url = e2e_env.gateway_url
    resp = post_spawn(gateway_url, {"spawner": "user"}, timeout=30.0)
    _ = resp.raise_for_status()
    other = cast(dict[str, int], resp.json())["id"]
    ids = _stub_conversation(page, agent)
    _ = page.add_init_script(_FRAME_PROBE)
    settle_ms = int(_AUTHORITY_DELAY_S * 1000)

    try:
        _ = page.goto(e2e_env.agent_url)
        _ = page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=15_000)
        expect(page.get_by_text("Reply 140", exact=True)).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("cold-load-ring")).to_have_count(0, timeout=15_000)
        viewport = page.locator('[data-slot="scroll-area-viewport"]').filter(
            has=page.get_by_role("log")
        )
        viewport.hover()
        page.mouse.wheel(0, -1500)
        page.wait_for_timeout(600)
        saved = cast(Place | None, viewport.evaluate(_FIRST_ROW))
        assert saved is not None and saved["id"] in ids, "the reader is not on a row in history"

        _sidebar_row(page, other).click()
        page.wait_for_url(re.compile(rf"[?&]agent_id={other}\b"))
        page.wait_for_timeout(500)
        _ = cast(object, page.evaluate("() => { window.__frames.length = 0; }"))
        _sidebar_row(page, agent).click()
        page.wait_for_url(re.compile(rf"[?&]agent_id={agent}\b"))
        page.wait_for_timeout(settle_ms + 800)
        frames = _frames(page)
        assert _assert_destination(frames, ids, saved, "switch back") > 0, (
            "the returning conversation never painted its rows"
        )
        first_shown = next(
            i
            for i, f in enumerate(frames)
            if f.get("rows") and not f.get("hidden") and f.get("id") in ids
        )
        # meiva:note
        # Retained rows open on the saved place at once: well inside the authority read's delay.
        assert first_shown < 20, (
            f"the saved place was first painted at frame {first_shown}, after the authority read"
        )

        _ = page.reload()
        _ = page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=15_000)
        page.wait_for_timeout(settle_ms + 1500)
        assert _assert_destination(_frames(page), ids, saved, "reload") > 0, (
            "the reloaded conversation never painted its rows"
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            _ = gateway_post(f"{gateway_url}/api/agents/{other}/terminate", timeout=5.0)
