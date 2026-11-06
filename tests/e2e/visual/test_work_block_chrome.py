# meiva:note
# The primary message rows stay free of floating controls in the browser.
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


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


# meiva:note
# Read primary message rows and inspect the expanded work block’s child rows.
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
  const primary = ['1.1', '1.90'].map((id) => document.querySelector(
    `.timeline-item[data-item-id="${id}"][data-message-role]`,
  ));
  if (primary.some((row) => !row)) return { error: 'primary fixture row missing' };
  return {
    rowCount: rows.length,
    primaryRoles: primary.map((row) => row.dataset.messageRole),
    primaryChromeCount: primary.filter((row) => row.querySelector('[data-testid="card-chrome"]')).length,
    turnChromeCount: turn.querySelectorAll('[data-testid="card-chrome"]').length,
    copyLabels: [...turn.querySelectorAll('button[aria-label]')].map((b) =>
      b.getAttribute('aria-label'),
    ),
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
    return ctx, page


def test_primary_messages_have_no_floating_chrome(
    playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_work_block(playwright_browser, frontend_target)
    try:
        measured = page.evaluate(MEASURE_JS)
        assert "error" not in measured, measured
        assert measured["rowCount"] >= 4, measured
        assert measured["primaryRoles"] == ["user", "assistant"], measured
        assert measured["primaryChromeCount"] == 0, measured
        assert measured["turnChromeCount"] == 0, measured
    finally:
        ctx.close()


# meiva:note
# Thinking, tool calls, and tool output carry no Copy control in the real DOM.
# meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
def test_tool_details_have_no_copy_controls(
    playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_work_block(playwright_browser, frontend_target)
    try:
        measured = page.evaluate(MEASURE_JS)
        assert "error" not in measured, measured
        labels = measured["copyLabels"]
        assert [label for label in labels if label and "copy" in label.lower()] == [], labels
    finally:
        ctx.close()
