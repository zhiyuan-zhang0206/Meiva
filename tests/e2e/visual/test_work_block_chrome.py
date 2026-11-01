# meiva:note
# The chrome band under a primary message row, plus the absence of below-card
# timestamp/copy chrome inside a work block's children, measured in a real
# engine: a primary `.timeline-item` pads its bottom by 1rem (16px) and hands
# that height back with a negative margin, so its hover band sits inside every
# clipping ancestor (`overflow` other than visible, or `content-visibility`
# paint containment), whereas secondary work items inside `[data-turn-id]` omit
# below-card timestamps and copy buttons.
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
    {"key": "display.show_timestamp_weekday", "value": True},
]


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


# meiva:note
# Read the primary message row's stamp and every ancestor that clips it (`overflow`
# other than visible, or `content-visibility` paint containment), and verify that
# the expanded work block's child rows mount neither below-card timestamps nor
# copy controls.
# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
MEASURE_JS = """
() => {
  const byMembers = (node) =>
    (node.getAttribute('data-turn-member-ids') || '').split(' ').filter(Boolean).length;
  const turns = [...document.querySelectorAll('[data-turn-id]')];
  const turn = turns.reduce((best, node) => (byMembers(node) > byMembers(best) ? node : best), turns[0]);
  if (!turn) return { error: 'no work block rendered' };
  const rows = [...turn.querySelectorAll('.timeline-item')];
  if (!rows.length) return { error: 'work block has no child row' };
  const turnChromeCount = turn.querySelectorAll('[data-testid="card-chrome"]').length;
  const humanRow = document.querySelector('.timeline-item[data-item-id="1.1"]');
  if (!humanRow) return { error: 'no primary human message row rendered' };
  const band = humanRow.querySelector('[data-testid="card-chrome"]');
  if (!band) return { error: 'primary human row mounted no chrome band' };
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
    turnChromeCount,
    stamp: { top: s.top, bottom: s.bottom, left: s.left, right: s.right },
    clips,
    copyLabels: [...turn.querySelectorAll('button[aria-label]')].map((b) =>
      b.getAttribute('aria-label'),
    ),
    humanCopyLabel: (() => {
      const btn = band.querySelector('button');
      return btn ? btn.getAttribute('aria-label') : null;
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
        """() => {
          for (const btn of document.querySelectorAll('[data-testid="turn-toggle"][data-expanded="false"]')) {
            btn.click();
          }
        }"""
    )
    page.wait_for_timeout(200)
    page.evaluate(
        """() => {
          for (const btn of document.querySelectorAll('[data-testid="card-toggle"][data-expanded="false"]')) {
            btn.click();
          }
        }"""
    )
    page.wait_for_timeout(300)
    page.evaluate(
        "() => document.querySelector('.timeline-item[data-item-id=\"1.1\"]').scrollIntoView({ block: 'center' })"
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
        assert measured["turnChromeCount"] == 0, measured
        stamp = measured["stamp"]
        clips = measured["clips"]
        assert clips, "the band must sit inside a clipping ancestor (row containment + viewport)"
        for clip in clips:
            assert clip["top"] <= stamp["top"] + TOLERANCE_PX, (clip, stamp)
            assert stamp["bottom"] <= clip["bottom"] + TOLERANCE_PX, (
                f"the primary message timestamp is clipped by {clip['name']}: "
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
