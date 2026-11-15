from __future__ import annotations

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual.stubbed_home import OVERRIDE_BASE_URL, new_context, open_page

_FORMULAS = (
    r"X\in\mathbb{R}^{B\times124\times10\times9}",
    r"\sqrt{\frac{1+x^2}{1-x^2}}",
    r"\frac{\sum_{i=0}^{n} a_i}{\prod_{j=0}^{m} b_j}",
)
_WIDE = r"\left(" + "+".join(rf"x_{{{i}}}^{{{i + 1}}}" for i in range(18)) + r"\right)"


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    _ = request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


@pytest.mark.parametrize("width", [1280, 390])
def test_display_math_preserves_ink_and_scrolls_wide_formulas(
    playwright_browser: Browser, frontend_target: str, width: int
) -> None:
    markdown = "\n\n".join(r"\[" + "\n" + formula + "\n" + r"\]" for formula in (*_FORMULAS, _WIDE))
    items = [{"item_id": "2.0", "kind": "agent_chat", "payload": markdown, "show_timestamp": False}]

    with new_context(playwright_browser, width) as ctx:
        page = open_page(
            ctx,
            frontend_target,
            "/?agent_id=1",
            stub_api=True,
            reconnect_sse=False,
            timeline_items=items,
        )
        blocks = page.locator(".chat-md .katex-display")
        expect(blocks).to_have_count(len(_FORMULAS) + 1)
        wide = blocks.last
        assert wide.evaluate("e => e.scrollWidth > e.clientWidth && e.clientWidth <= innerWidth"), (
            "a wide equation must scroll within its own box"
        )
        assert wide.evaluate("e => { e.scrollLeft = e.scrollWidth; return e.scrollLeft > 0; }")
        _ = wide.evaluate("e => { e.scrollLeft = 0; }")

        # meiva:note
        # With fonts loaded, compare the same message against its unclipped
        # rendering. The DOM alone cannot detect ink outside the line box.
        _ = page.evaluate("document.fonts.ready")
        prose = blocks.first.locator("..")
        actual = prose.screenshot(animations="disabled")
        _ = blocks.evaluate_all(
            "els => els.slice(0, -1).forEach(e => e.style.overflow = 'visible')"
        )
        assert prose.screenshot(animations="disabled") == actual, (
            "display math lost ink to clipping"
        )
