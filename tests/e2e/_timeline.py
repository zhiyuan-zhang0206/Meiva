# meiva:note
# A bare text wait reports one symptom for four different states: the display
# mode never applied (a collapsed block renders no row and no payload), the
# virtual window never moved to the newest rank, the reader was left above the
# tail, or the payload has not painted yet. These helpers wait on each layer
# with its own budget and raise with the DOM snapshot that names the one that
# failed, so a red run carries its own diagnosis.
# meiva:link ui/web/src/docs/frontend-components/timeline-view/bounded-window.meiva.okf.md

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# meiva:note
# Budgets measured from `sse-ready` to the newest row: 0.8-1.3 s on an idle
# 16-core host, 8.2 s under 16x CPU throttling, 19.4 s under 32x, where the page
# load rather than the mount is what grows. The premise is retried once instead
# of waited out, which keeps both budgets short enough to fail fast and bounds a
# stuck mount at two attempts rather than one long wait.
# meiva:link ui/web/src/docs/frontend-components/timeline-view/bounded-window.meiva.okf.md
PREMISE_TIMEOUT_MS = 10_000
PIN_TIMEOUT_MS = 5_000

_PROBE_JS = """(itemId) => {
  const viewport = document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]');
  const rows = viewport ? [...viewport.querySelectorAll('.timeline-item[data-item-id]')] : [];
  const collapsed = viewport ? [...viewport.querySelectorAll('[data-turn-expanded="false"][data-item-id]')] : [];
  const tail = viewport?.querySelector(`.timeline-item[data-item-id="${CSS.escape(itemId)}"]`);
  const tailBox = tail?.getBoundingClientRect();
  const viewBox = viewport?.getBoundingClientRect();
  return {
    rows: rows.length,
    collapsedRows: collapsed.length,
    firstId: rows.length ? rows[0].dataset.itemId : null,
    lastId: rows.length ? rows[rows.length - 1].dataset.itemId : null,
    tailMounted: Boolean(tail),
    tailVisible: Boolean(tailBox && viewBox && tailBox.bottom <= viewBox.bottom + 1 && tailBox.top >= viewBox.top - 1),
    scrollTop: viewport ? Math.round(viewport.scrollTop) : null,
    scrollHeight: viewport ? Math.round(viewport.scrollHeight) : null,
    clientHeight: viewport ? Math.round(viewport.clientHeight) : null,
    distancePx: viewport ? Math.round(viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop) : null,
    spacers: viewport ? [...viewport.querySelectorAll('[data-timeline-spacer]')].map((node) => node.clientHeight) : [],
  };
}"""


def probe_timeline(page: Page, item_id: str) -> dict[str, Any]:
    return page.evaluate(_PROBE_JS, item_id)


# meiva:note
# The strict "distance to the bottom is zero" assertion stays where the test
# already makes it, after an explicit scroll-to-bottom, so a reader parked a
# little short of the tail still exercises the window here instead of failing.
# meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
def wait_for_parked_tail(page: Page, item_id: str, payload: str) -> None:
    attempts: list[dict[str, Any]] = []
    for attempt in range(2):
        try:
            page.wait_for_function(
                """() => {
                  const viewport = document.querySelector('[role=log]')
                    ?.closest('[data-slot=scroll-area-viewport]');
                  return Boolean(viewport?.querySelector('.timeline-item[data-item-id]'));
                }""",
                timeout=PREMISE_TIMEOUT_MS,
            )
            break
        except PlaywrightTimeoutError:
            attempts.append(probe_timeline(page, item_id))
            if attempt:
                raise AssertionError(
                    (
                        "the timeline never rendered primary message rows",
                        attempts,
                    )
                ) from None
            page.reload()
            page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=15_000)
    try:
        page.wait_for_function(
            '(id) => Boolean(document.querySelector(`.timeline-item[data-item-id="${CSS.escape(id)}"]`))',
            arg=item_id,
            timeout=PIN_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        raise AssertionError(
            (
                "the newest row never mounted: the window did not move to the newest rank",
                probe_timeline(page, item_id),
            )
        ) from None
    try:
        page.locator(f'.timeline-item[data-item-id="{item_id}"]').get_by_text(
            payload, exact=False
        ).wait_for(state="visible", timeout=PIN_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        raise AssertionError(
            ("the newest payload is mounted but not readable", probe_timeline(page, item_id))
        ) from None


_MOVE_TO_TOP_JS = """() => {
  const viewport = document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]');
  if (!viewport) throw new Error('timeline viewport missing');
  viewport.scrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight - 400);
  viewport.dispatchEvent(new Event('scroll'));
  viewport.scrollTop = 0;
  viewport.dispatchEvent(new Event('scroll'));
}"""

_A_ROW_IN_VIEW_JS = """() => {
  const viewport = document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]');
  if (!viewport) return false;
  const view = viewport.getBoundingClientRect();
  return [...viewport.querySelectorAll('.timeline-item')].some((row) => {
    const box = row.getBoundingClientRect();
    return box.bottom > view.top && box.top < view.bottom;
  });
}"""

_READ_ANCHOR_JS = """() => {
  const viewport = document.querySelector('[role=log]')?.closest('[data-slot=scroll-area-viewport]');
  const view = viewport.getBoundingClientRect();
  const row = [...viewport.querySelectorAll('.timeline-item')]
    .find((node) => node.getBoundingClientRect().bottom > view.top);
  if (!row) throw new Error('no visible reading anchor');
  return { id: row.dataset.itemId, viewportOffsetPx: row.getBoundingClientRect().top - view.top };
}"""


# meiva:note
# The window re-renders around the new position; until it does, every mounted row
# still sits where the previous window left it, so reading the anchor in the same
# frame returns a row thousands of pixels away from the viewport that a later
# assertion cannot mean anything about (#5678 signature B).
# meiva:link ui/web/src/docs/frontend-components/timeline-view/bounded-window.meiva.okf.md
def move_to_top_and_read_anchor(page: Page) -> dict[str, Any]:
    page.evaluate(_MOVE_TO_TOP_JS)
    page.wait_for_function(_A_ROW_IN_VIEW_JS, timeout=PIN_TIMEOUT_MS)
    return page.evaluate(_READ_ANCHOR_JS)
