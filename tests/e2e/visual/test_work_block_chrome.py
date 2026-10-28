# meiva:note
# The chrome band under a work block's last child, measured in a real engine: a
# row pads its bottom by the 12px gap between blocks and hands that height back
# with a negative margin, so the work block's clipping collapse wrapper must
# reserve the same 12px — at 8px the clip line fell 4px inside the band and
# shaved the bottom of the last block's stamp (user 2026-10-29). jsdom has no
# layout engine, so the guard is a rect comparison here: a sub-pixel shortfall is
# not a defect, a whole clipped pixel is.
# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md

from __future__ import annotations

import pytest
from playwright.sync_api import Browser

from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual.stubbed_home import (
    OVERRIDE_BASE_URL,
    new_context,
    open_page,
    work_block_items,
)

TOLERANCE_PX = 1
EXPANDED_SETTINGS: list[dict[str, object]] = [
    {"key": "display.expand_runs_mode", "value": "all"},
    {"key": "display.show_timestamp_weekday", "value": True},
]


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


# meiva:note
# Read the last child's stamp and every ancestor that clips it: `overflow` other
# than visible, or `content-visibility` — the row's own paint containment. Both
# are boundaries the band must stay inside.
# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
MEASURE_JS = """
() => {
  const byMembers = (node) =>
    (node.getAttribute('data-turn-member-ids') || '').split(' ').filter(Boolean).length;
  const turns = [...document.querySelectorAll('[data-turn-id]')];
  const turn = turns.reduce((best, node) => (byMembers(node) > byMembers(best) ? node : best), turns[0]);
  if (!turn) return { error: 'no work block rendered' };
  const rows = [...turn.querySelectorAll('.timeline-item')];
  const last = rows[rows.length - 1];
  if (!last) return { error: 'work block has no child row' };
  const band = last.querySelector('[data-testid="card-chrome"]');
  if (!band) return { error: 'last child mounted no chrome band' };
  const stamp = band.querySelector('span');
  if (!stamp) return { error: 'chrome band holds no timestamp' };
  const s = stamp.getBoundingClientRect();
  const clips = [];
  for (let node = stamp.parentElement; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    const overflow = [style.overflow, style.overflowX, style.overflowY];
    const clipsByOverflow = overflow.some((value) => value !== 'visible');
    const clipsByContainment = style.contentVisibility !== 'visible';
    if (!clipsByOverflow && !clipsByContainment) continue;
    const r = node.getBoundingClientRect();
    clips.push({
      name: node.getAttribute('data-testid') || node.className || node.tagName,
      top: r.top,
      bottom: r.bottom,
      left: r.left,
      right: r.right,
      overflow: style.overflow,
    });
  }
  return {
    rowCount: rows.length,
    stamp: { top: s.top, bottom: s.bottom, left: s.left, right: s.right },
    clips,
    copyLabels: [...turn.querySelectorAll('button[aria-label]')].map((b) =>
      b.getAttribute('aria-label'),
    ),
    humanCopyLabel: (() => {
      const human = document.querySelector('[data-item-id="1.1"] [data-testid="card-chrome"] button');
      return human ? human.getAttribute('aria-label') : null;
    })(),
  };
}
"""


# meiva:note
# The suite always stubs `/api/**` and the event stream, so a deployed bundle
# (`OVERRIDE_BASE_URL`) answers the same rows as the local build: these tests measure
# their own fixture, never the deployment's data.
# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
def _open_work_block(browser: Browser, base_url: str, width: int = 1280):
    ctx = new_context(browser, width)
    page = open_page(
        ctx,
        base_url,
        "/",
        stub_api=True,
        timeline_items=work_block_items(),
        settings=EXPANDED_SETTINGS,
    )
    page.wait_for_selector("[data-turn-id]", timeout=15_000)
    page.wait_for_timeout(1500)
    page.evaluate(
        "() => document.querySelector('[data-turn-id]').scrollIntoView({ block: 'center' })"
    )
    page.wait_for_timeout(300)
    return ctx, page


def test_last_block_timestamp_is_not_clipped(
    playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_work_block(playwright_browser, frontend_target)
    try:
        measured = page.evaluate(MEASURE_JS)
        assert "error" not in measured, measured
        assert measured["rowCount"] >= 4, measured
        stamp = measured["stamp"]
        clips = measured["clips"]
        assert clips, "the band must sit inside a clipping ancestor (the collapse wrapper)"
        for clip in clips:
            assert clip["top"] <= stamp["top"] + TOLERANCE_PX, (clip, stamp)
            assert stamp["bottom"] <= clip["bottom"] + TOLERANCE_PX, (
                f"the last block's timestamp is clipped by {clip['name']}: "
                f"stamp bottom {stamp['bottom']:.2f} vs clip bottom {clip['bottom']:.2f}"
            )
    finally:
        ctx.close()


# meiva:note
# Thinking / tool call / tool output carry no copy control in the real DOM: the
# jsdom suite owns the per-kind table, and this asserts the same rule survives the
# real renderer — PythonCode's own control and the hover band alike.
# meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
def test_copy_belongs_to_message_text_only(
    playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_work_block(playwright_browser, frontend_target)
    try:
        measured = page.evaluate(MEASURE_JS)
        assert "error" not in measured, measured
        labels = measured["copyLabels"]
        assert [label for label in labels if label and "copy" in label.lower()] == [], labels
        assert measured["humanCopyLabel"] == "Copy message", measured
    finally:
        ctx.close()
