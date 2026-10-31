from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TypedDict, cast

import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect

from scripts.post_deploy_visual.layout_assertions import (
    all_elements_within_parents,
    element_within_parent,
    element_within_viewport,
    no_document_horizontal_overflow,
)
from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual.stubbed_home import (
    OVERRIDE_BASE_URL,
    new_context,
    open_page,
    timeline_snapshot,
    wait_layout_settled,
)

# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
VIEWPORTS = [320, 390, 768]


@pytest.fixture(scope="session")
def frontend_target(request: pytest.FixtureRequest) -> str:
    if OVERRIDE_BASE_URL:
        return OVERRIDE_BASE_URL
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


def _no_page_scroll(page: Page) -> bool:
    return no_document_horizontal_overflow(page)


def _surface_within_parent(page: Page) -> bool:
    return element_within_parent(page, '[data-testid="timeline-surface"]')


def _composer_within_viewport(page: Page) -> bool:
    return element_within_viewport(page, "textarea")


def _no_page_vertical_scroll(page: Page) -> bool:
    return page.evaluate(
        "() => document.scrollingElement.scrollHeight <= document.scrollingElement.clientHeight + 1"
    )


def _inbox_rows_within_container(page: Page) -> bool:
    return all_elements_within_parents(page, '[data-testid="inbox-row"]')


@pytest.mark.parametrize("width", VIEWPORTS)
def test_timeline_layout_invariants(
    width: int, playwright_browser: Browser, frontend_target: str
) -> None:
    ctx = new_context(playwright_browser, width)
    try:
        page = open_page(ctx, frontend_target, "/", stub_api=not OVERRIDE_BASE_URL)
        wait_layout_settled(page)
        assert _no_page_scroll(page), "I1: page-level horizontal scroll (timeline)"
        assert _surface_within_parent(page), "I2: timeline-surface wider than parent (#979)"
        assert _composer_within_viewport(page), "I3: composer overflows viewport"
        assert _no_page_vertical_scroll(page), "I6: page scrolls as a whole"
    finally:
        ctx.close()


def _assert_scrollbar_state(page: Page, track: Locator, opacity: str, state: str) -> None:
    expect(track).to_have_css("opacity", opacity, timeout=3000)
    assert _no_page_scroll(page), f"{state} scrollbar widened the document"


def _invisible_track_point(track: Locator) -> tuple[float, float]:
    box = track.bounding_box()
    assert box is not None
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    assert track.evaluate(
        "(el, point) => { const hit = document.elementFromPoint(point.x, point.y); return hit === el || el.contains(hit); }",
        {"x": x, "y": y},
    ), "invisible track is not hit-testable"
    return x, y


def _assert_scrollbar_layout(viewport: Locator, content: Locator, before: dict) -> None:
    assert viewport.evaluate("el => el.clientWidth") == before["width"]
    assert content.evaluate("el => el.getBoundingClientRect().toJSON()") == before["rect"]


def _content_hover_point(viewport: Locator) -> tuple[float, float]:
    box = viewport.bounding_box()
    assert box is not None
    x = box["x"] + min(80, box["width"] / 2)
    y = box["y"] + box["height"] / 2
    assert viewport.evaluate(
        "(el, point) => el.contains(document.elementFromPoint(point.x, point.y))",
        {"x": x, "y": y},
    ), "content hover point is outside the viewport"
    return x, y


def _capture_inspector_states(page: Page, panel: Locator, prefix: str) -> None:
    evidence = Path(__file__).resolve().parents[3] / "tmp" / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    scroller = panel.locator('[data-slot="scroll-area-viewport"], .overflow-y-auto').last
    box = panel.bounding_box()
    assert box is not None

    page.mouse.move(1, 1)
    page.wait_for_timeout(1200)
    page.screenshot(path=str(evidence / f"{prefix}-rest.png"))

    page.mouse.move(box["x"] + box["width"] - 5, box["y"] + box["height"] / 2)
    page.wait_for_timeout(350)
    page.screenshot(path=str(evidence / f"{prefix}-hover.png"))

    page.mouse.move(box["x"] + 80, box["y"] + box["height"] / 2)
    before = scroller.evaluate("el => el.scrollTop")
    page.mouse.wheel(0, 400)
    page.wait_for_function(
        "([el, before]) => el.scrollTop !== before",
        arg=[scroller.element_handle(), before],
    )
    page.screenshot(path=str(evidence / f"{prefix}-scroll.png"))


# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
def test_timeline_scrollbar_hover_reveal(playwright_browser: Browser, frontend_target: str) -> None:
    ctx = new_context(playwright_browser, 1280)
    try:
        page = open_page(
            ctx, frontend_target, "/", stub_api=not OVERRIDE_BASE_URL, reconnect_sse=False
        )
        wait_layout_settled(page)
        assert page.evaluate("matchMedia('(hover: hover)').matches")

        surface = page.locator('[data-testid="timeline-surface"]')
        viewport = surface.locator('[data-slot="scroll-area-viewport"]').first
        track = surface.locator('[data-slot="scroll-area-scrollbar"]').first
        content = viewport.locator('[role="log"]')
        expect(track).to_be_attached()
        assert viewport.evaluate("el => el.scrollHeight > el.clientHeight")

        page.mouse.move(1, 1)
        _assert_scrollbar_state(page, track, "0", "at rest")
        before = viewport.evaluate(
            "el => ({width: el.clientWidth, rect: el.querySelector('[role=log]').getBoundingClientRect().toJSON()})"
        )

        track_x, track_y = _invisible_track_point(track)
        page.mouse.move(track_x, track_y)
        _assert_scrollbar_state(page, track, "1", "hovered")
        _assert_scrollbar_layout(viewport, content, before)

        page.mouse.move(1, 1)
        _assert_scrollbar_state(page, track, "0", "hidden")

        content_x, content_y = _content_hover_point(viewport)
        page.mouse.move(content_x, content_y)
        page.wait_for_timeout(350)
        _assert_scrollbar_state(page, track, "0", "content hover")

        scroll_top = viewport.evaluate("el => el.scrollTop")
        max_scroll = viewport.evaluate("el => el.scrollHeight - el.clientHeight")
        page.mouse.wheel(0, 400 if scroll_top < max_scroll / 2 else -400)
        page.wait_for_function(
            "([el, before]) => el.scrollTop !== before",
            arg=[viewport.element_handle(), scroll_top],
        )
        page.wait_for_function(
            "el => Number(getComputedStyle(el).opacity) > 0.95",
            arg=track.element_handle(),
            timeout=2000,
        )
        assert _no_page_scroll(page), "scroll reveal widened the document"
        _assert_scrollbar_state(page, track, "0", "idle hide")
    finally:
        ctx.close()


def test_inspector_scrollbar_hover_reveal(
    playwright_browser: Browser, frontend_target: str
) -> None:
    if OVERRIDE_BASE_URL:
        pytest.skip("the inspector scroll matrix requires deterministic stubbed sections")
    ctx = new_context(playwright_browser, 1280)
    try:
        page = open_page(
            ctx,
            frontend_target,
            "/",
            stub_api=not OVERRIDE_BASE_URL,
            reconnect_sse=False,
            open_inspector=True,
        )
        wait_layout_settled(page)
        assert page.evaluate("matchMedia('(hover: hover)').matches")
        panel = page.locator('[data-testid="shared-inspector"]')
        expect(panel).to_be_visible()
        expect(page.locator('[data-testid="timeline-surface"]')).to_be_visible()
        prefix = os.environ.get("MEIVA_SCROLLBAR_EVIDENCE_PREFIX")
        if prefix:
            _capture_inspector_states(page, panel, prefix)

        surface = panel.locator('[data-slot="scroll-area"]')
        viewport = surface.locator('[data-slot="scroll-area-viewport"]')
        track = surface.locator('[data-slot="scroll-area-scrollbar"]')
        content = viewport.locator(":scope > div > div")
        expect(surface).to_be_attached()
        expect(track).to_be_attached()
        assert viewport.evaluate("el => el.scrollHeight > el.clientHeight")
        assert viewport.evaluate("el => el.offsetWidth === el.clientWidth"), (
            "inspector viewport reserves width for a native scrollbar"
        )
        thumb_class = track.locator('[data-slot="scroll-area-thumb"]').get_attribute("class")
        assert thumb_class is not None and "bg-border" in thumb_class

        page.mouse.move(1, 1)
        _assert_scrollbar_state(page, track, "0", "inspector at rest")
        before = {
            "width": viewport.evaluate("el => el.clientWidth"),
            "rect": content.evaluate("el => el.getBoundingClientRect().toJSON()"),
        }

        track_x, track_y = _invisible_track_point(track)
        page.mouse.move(track_x, track_y)
        _assert_scrollbar_state(page, track, "1", "inspector hovered")
        _assert_scrollbar_layout(viewport, content, before)

        page.mouse.move(1, 1)
        _assert_scrollbar_state(page, track, "0", "inspector hidden")
        content_x, content_y = _content_hover_point(viewport)
        page.mouse.move(content_x, content_y)
        page.wait_for_timeout(350)
        _assert_scrollbar_state(page, track, "0", "inspector content hover")

        scroll_top = viewport.evaluate("el => el.scrollTop")
        max_scroll = viewport.evaluate("el => el.scrollHeight - el.clientHeight")
        page.mouse.wheel(0, 400 if scroll_top < max_scroll / 2 else -400)
        page.wait_for_function(
            "([el, before]) => el.scrollTop !== before",
            arg=[viewport.element_handle(), scroll_top],
        )
        page.wait_for_function(
            "el => Number(getComputedStyle(el).opacity) > 0.95",
            arg=track.element_handle(),
            timeout=2000,
        )
        assert viewport.evaluate("el => el.clientWidth") == before["width"]
        assert _no_page_scroll(page), "inspector scroll reveal widened the document"
        _assert_scrollbar_state(page, track, "0", "inspector idle hide")
    finally:
        ctx.close()


@pytest.mark.parametrize("width", VIEWPORTS)
def test_fleet_layout_invariants(
    width: int, playwright_browser: Browser, frontend_target: str
) -> None:
    ctx = new_context(playwright_browser, width)
    try:
        page = open_page(ctx, frontend_target, "/fleet", stub_api=not OVERRIDE_BASE_URL)
        wait_layout_settled(page)
        assert _no_page_scroll(page), "I1: page-level horizontal scroll (fleet)"

        page.locator('[role="tab"]', has_text="Tasks").click()
        page.wait_for_selector('button:has-text("Kanban")', timeout=15_000)
        page.wait_for_timeout(800)
        assert _no_page_scroll(page), "I4: Tasks toolbar widens the page"

        page.locator('[role="tab"]', has_text="Inbox").click()
        page.wait_for_timeout(1200)
        assert _no_page_scroll(page), "I1: long-title inbox row widens the page"
        if OVERRIDE_BASE_URL:
            if page.locator('[data-testid="inbox-row"]').count() > 0:
                assert _inbox_rows_within_container(page), "I5: inbox row overflows"
        else:
            assert _inbox_rows_within_container(page), "I5: inbox row overflows"
    finally:
        ctx.close()


# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
VIEWPORT_HEIGHT = 664
KEYBOARD_HEIGHT = 480


_FAKE_VISUAL_VIEWPORT_JS = """
(() => {
  const listeners = { resize: new Set(), scroll: new Set() };
  let state = { height: window.innerHeight, scale: 1, offsetTop: 0 };
  const viewport = {
    get height() { return state.height; },
    get width() { return window.innerWidth; },
    get scale() { return state.scale; },
    get offsetTop() { return state.offsetTop; },
    get offsetLeft() { return 0; },
    get pageTop() { return state.offsetTop; },
    get pageLeft() { return 0; },
    addEventListener(type, handler) { listeners[type]?.add(handler); },
    removeEventListener(type, handler) { listeners[type]?.delete(handler); },
  };
  const install = (target) =>
    Object.defineProperty(target, "visualViewport", { configurable: true, get: () => viewport });
  try {
    install(window);
  } catch (error) {
    install(Window.prototype);
  }
  window.__visualViewport = {
    set(height, scale = 1) {
      state = { ...state, height, scale };
      for (const handler of listeners.resize) handler(new Event("resize"));
    },
  };
})();
"""


def _set_visual_viewport(page: Page, height: int, *, scale: float = 1) -> None:
    page.evaluate(
        "([height, scale]) => window.__visualViewport.set(height, scale)", [height, scale]
    )


def _shell_height_style(page: Page) -> str:
    return page.evaluate("() => document.documentElement.style.height")


def _open_keyboard_page(playwright_browser: Browser, base_url: str) -> Page:
    ctx = new_context(playwright_browser, 390)
    page = open_page(
        ctx,
        base_url,
        "/",
        stub_api=not OVERRIDE_BASE_URL,
        reconnect_sse=False,
        extra_init_script=_FAKE_VISUAL_VIEWPORT_JS,
    )
    wait_layout_settled(page)
    return page


def test_viewport_meta_asks_chrome_to_resize_content(
    playwright_browser: Browser, frontend_target: str
) -> None:
    page = _open_keyboard_page(playwright_browser, frontend_target)
    ctx = page.context
    try:
        content = page.locator('meta[name="viewport"]').get_attribute("content")
        assert content is not None and "interactive-widget=resizes-content" in content, content
    finally:
        ctx.close()


def test_composer_follows_the_keyboard(playwright_browser: Browser, frontend_target: str) -> None:
    page = _open_keyboard_page(playwright_browser, frontend_target)
    ctx = page.context
    try:
        _set_visual_viewport(page, KEYBOARD_HEIGHT)
        assert _shell_height_style(page) == f"{KEYBOARD_HEIGHT}px"

        box = page.locator('[data-testid="composer"]').bounding_box()
        assert box is not None
        assert box["y"] + box["height"] <= KEYBOARD_HEIGHT + 1, (
            f"composer bottom at {box['y'] + box['height']} sits below the "
            f"{KEYBOARD_HEIGHT}px visible band — under the keyboard"
        )
    finally:
        ctx.close()


def test_keyboard_edges_leave_the_shell_intact(
    playwright_browser: Browser, frontend_target: str
) -> None:
    page = _open_keyboard_page(playwright_browser, frontend_target)
    ctx = page.context
    try:
        _set_visual_viewport(page, VIEWPORT_HEIGHT - 100)
        assert _shell_height_style(page) == ""

        _set_visual_viewport(page, KEYBOARD_HEIGHT)
        assert _shell_height_style(page) == f"{KEYBOARD_HEIGHT}px"
        _set_visual_viewport(page, KEYBOARD_HEIGHT, scale=2)
        assert _shell_height_style(page) == ""

        _set_visual_viewport(page, KEYBOARD_HEIGHT)
        assert _shell_height_style(page) == f"{KEYBOARD_HEIGHT}px"
        _set_visual_viewport(page, VIEWPORT_HEIGHT)
        assert _shell_height_style(page) == ""
    finally:
        ctx.close()


_STICKY_SURFACE = """el => {
  const style = getComputedStyle(el);
  const top = getComputedStyle(el, '::before');
  const line = getComputedStyle(el, '::after');
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d');
  const alpha = color => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    return ctx.getImageData(0, 0, 1, 1).data[3];
  };
  return {
    rect: el.getBoundingClientRect().toJSON(),
    alpha: alpha(style.backgroundColor),
    paneAlpha: alpha(top.backgroundColor),
    paneBlur: top.backdropFilter,
    paneZ: top.zIndex,
    paneBottom: parseFloat(top.bottom),
    topStart: parseFloat(top.top),
    lineBottom: parseFloat(line.bottom),
    lineHeight: parseFloat(line.height),
  };
}"""


_PANE_BEFORE_COLOR = "el => getComputedStyle(el, '::before').backgroundColor"
_PANE_BEFORE_TRANSITION_MS = (
    "el => parseFloat(getComputedStyle(el, '::before').transitionDuration) * 1000"
)


def _wait_for_pane_settled(page: Page, header_selector: str, *, poll_ms: int = 20) -> None:
    # meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
    locator = page.locator(header_selector)
    duration_ms = locator.evaluate(_PANE_BEFORE_TRANSITION_MS)
    page.wait_for_timeout(duration_ms)
    deadline = time.monotonic() + (duration_ms * 2 + 500) / 1000
    previous = locator.evaluate(_PANE_BEFORE_COLOR)
    while time.monotonic() < deadline:
        page.wait_for_timeout(poll_ms)
        current = locator.evaluate(_PANE_BEFORE_COLOR)
        if current == previous:
            return
        previous = current


def _assert_sticky_sealed(
    page: Page, header_selector: str, *, hovered: bool = False
) -> dict[str, float]:
    surface = page.locator(header_selector).evaluate(_STICKY_SURFACE)
    assert surface["alpha"] == 0, "button background flattens the frosted pane"
    expected_alpha = 77 if hovered else 242
    assert abs(surface["paneAlpha"] - expected_alpha) <= 1, "glass tint changed"
    assert surface["paneBlur"] == "blur(12px)", "backdrop blur is missing"
    assert surface["paneZ"] == "-10", "pane must stay behind the label"
    assert surface["topStart"] <= -1 and surface["paneBottom"] == 0
    assert surface["lineBottom"] == 0, "separator is detached from the header edge"
    assert surface["lineHeight"] == 1
    return surface["rect"]


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_pinned_work_detail_seal(
    width: int, theme: str, playwright_browser: Browser, frontend_target: str
) -> None:
    context = new_context(playwright_browser, width)
    try:
        page = open_page(
            context,
            frontend_target,
            "/",
            stub_api=not OVERRIDE_BASE_URL,
            reconnect_sse=False,
        )
        wait_layout_settled(page)
        page.evaluate(
            "theme => document.documentElement.classList.toggle('dark', theme === 'dark')", theme
        )
        turn = page.locator("[data-turn-expanded]").first
        turn_toggle = turn.locator('[data-testid="turn-toggle"]')
        if turn_toggle.get_attribute("aria-expanded") == "false":
            turn_toggle.click()
            expect(turn_toggle).to_have_attribute("aria-expanded", "true")
            page.wait_for_timeout(250)
        row = page.locator('.timeline-item[data-item-id="0.0"]')
        expect(row).to_be_visible()
        header_selector = '.timeline-item[data-item-id="0.0"] [data-testid="card-toggle"]'
        header = page.locator(header_selector)
        if header.get_attribute("aria-expanded") == "false":
            header.click()
            expect(header).to_have_attribute("aria-expanded", "true")
        row.locator("div").first.evaluate("el => { el.style.minHeight = '1200px'; }")
        row.evaluate("""el => {
          const viewport = el.closest('[data-radix-scroll-area-viewport]');
          viewport.scrollTop += el.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top + 100;
        }""")
        expect(header).to_have_attribute("data-stuck", "true")
        page.mouse.move(1, 1)
        _wait_for_pane_settled(page, header_selector)
        resting = _assert_sticky_sealed(page, header_selector)
        bar = page.locator('[data-testid="timeline-surface"] header').bounding_box()
        assert bar is not None
        turn_box = turn_toggle.bounding_box()
        assert turn_box is not None
        assert abs(turn_box["y"] - (bar["y"] + bar["height"])) < 0.5
        assert abs(resting["top"] - (turn_box["y"] + turn_box["height"])) < 0.5
        for item_id in ("1.1", "1.2"):
            primary_row = page.locator(f'.timeline-item[data-item-id="{item_id}"]')
            assert primary_row.locator('[data-testid="card-toggle"]').count() == 0
            assert primary_row.evaluate("el => getComputedStyle(el).position") not in (
                "sticky",
                "fixed",
            )
        header.hover()
        _wait_for_pane_settled(page, header_selector)
        assert _assert_sticky_sealed(page, header_selector, hovered=True) == resting, (
            "hover changed pin geometry"
        )
    finally:
        context.close()


# meiva:note
# Both home columns end with the same bottom band, so their rules are one
# horizontal line at rest — and only at rest: the composer's band grows with the
# draft and the sidebar footer keeps the resting band, so a grown composer leaves
# the footer's rule where it was (user report 2026-10-29 replaced the day the
# footer tracked the composer's published height). The gap above the composer's
# rule is the list's share (`pb-6`, 24px) plus the composer's (`pt-0.5`, 2px).
# The jsdom half of these contracts is in composer.test.tsx / footer.test.tsx /
# header-bar.test.tsx — those pin the classes, these measure the pixels.
# meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
BOTTOM_BAND_VIEWPORTS = [(768, 900), (1024, 900), (1440, 900), (1920, 1080)]
BOTTOM_RULE_GAP_PX = 26
SHELL_ICON_PX = 16


class IconBox(TypedDict):
    w: float
    h: float
    label: str


def _open_stubbed(
    playwright_browser: Browser, width: int, height: int, base_url: str
) -> tuple[BrowserContext, Page]:
    ctx = playwright_browser.new_context(viewport={"width": width, "height": height})
    page = open_page(ctx, base_url, "/", stub_api=not OVERRIDE_BASE_URL)
    wait_layout_settled(page)
    return ctx, page


@pytest.mark.parametrize("width,height", BOTTOM_BAND_VIEWPORTS)
def test_bottom_rule_is_one_line(
    width: int, height: int, playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_stubbed(playwright_browser, width, height, frontend_target)
    try:
        footer = page.locator('[data-testid="sidebar-footer"]')
        band = page.locator('[data-testid="composer-band"]')
        expect(footer).to_be_visible()
        expect(band).to_be_visible()
        footer_box = footer.bounding_box()
        band_box = band.bounding_box()
        assert footer_box is not None and band_box is not None
        assert abs(footer_box["y"] - band_box["y"]) <= 1, (
            f"bottom rules are {abs(footer_box['y'] - band_box['y']):.2f}px apart at {width}px: "
            f"sidebar footer y={footer_box['y']}, composer band y={band_box['y']} (tolerance 1px: "
            "a percentage-wide panel can round a half-pixel box edge per column; two distinct "
            "painted lines are never closer than 1px)"
        )
        assert abs(footer_box["height"] - band_box["height"]) <= 1
        page.locator('[data-testid="composer-input"]').fill("draft line\n" * 10)
        page.wait_for_timeout(300)
        grown = page.locator('[data-testid="composer-band"]').bounding_box()
        followed = footer.bounding_box()
        assert grown is not None and followed is not None
        assert grown["height"] > band_box["height"], (
            "the composer band did not grow with the draft, so the grown-state check proves nothing"
        )
        assert abs(followed["y"] - footer_box["y"]) <= 1, (
            f"the sidebar rule moved with the composer's: it was y={footer_box['y']} at rest "
            f"and y={followed['y']} under a grown composer, while the band rose to "
            f"y={grown['y']} (the sidebar keeps the resting band — user report 2026-10-29)"
        )
    finally:
        ctx.close()


@pytest.mark.parametrize("width,height", [(390, 844), (768, 900), (1440, 900)])
def test_bottom_rule_gap_is_halved(
    width: int, height: int, playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_stubbed(playwright_browser, width, height, frontend_target)
    try:
        viewport = page.locator(
            '[data-testid="timeline-surface"] [data-slot="scroll-area-viewport"]'
        ).first
        viewport.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        page.wait_for_timeout(400)
        band_box = page.locator('[data-testid="composer-band"]').bounding_box()
        row_box = page.locator(".timeline-item").last.bounding_box()
        assert band_box is not None and row_box is not None
        gap = band_box["y"] - (row_box["y"] + row_box["height"])
        assert abs(gap - BOTTOM_RULE_GAP_PX) <= 1, (
            f"gap between the last row and the composer's rule is {gap:.2f}px at {width}px, "
            f"expected {BOTTOM_RULE_GAP_PX}px +-1 (the timeline's pb-6 plus the composer's "
            "pt-0.5; a row's own height is fractional, hence the 1px tolerance)"
        )
    finally:
        ctx.close()


# meiva:note
# A streaming turn re-renders this surface at the turn's own pace — the 0.5 s
# `llm_keepalive` beats make that rate a property of the protocol. The bottom rule
# is the floor of the screen: chunks arriving must not move it. The stream here is
# driven from Python (`scripted_sse`), so "after 24 chunks" is a fact about the
# events fed rather than about a sleep, and the assertion is a distance in pixels
# rather than a frame count.
#
# The second case is the one that can fail: a draft grows the composer band, and a
# sidebar rule that follows the composer (the coupling the 2026-10-29 report
# removed) jumps with it. Restoring `min-height: var(--composer-band-height)` on
# the footer turns that case red; with the coupling gone the footer holds the
# resting band through the draft and through every chunk.
#
# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
STREAM_VIEWPORTS = [(1024, 900), (1440, 900)]
STREAM_CHUNKS = 24
DRAFT_LINES = 10
BOTTOM_RULE_BOX_JS = """() => {
  const box = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return { y: rect.y, height: rect.height };
  };
  return { band: box('[data-testid="composer-band"]'), footer: box('[data-testid="sidebar-footer"]') };
}"""


class BottomRuleBox(TypedDict):
    y: float
    height: float


def _measure_bottom_rule(page: Page) -> dict[str, BottomRuleBox]:
    boxes = page.evaluate(BOTTOM_RULE_BOX_JS)
    assert boxes["band"] is not None and boxes["footer"] is not None, (
        "the bottom rule is not on screen, so nothing was measured"
    )
    return cast("dict[str, BottomRuleBox]", boxes)


# meiva:note
# The API is stubbed even against an overridden bundle: this suite owns the
# conversation through the scripted stream, and a live backend would answer with
# a different one. The page is not measured until the stream reports a bound
# handler and the seeded snapshot is on screen, so a dropped event fails loudly
# instead of leaving the geometry an assertion about nothing.
# meiva:link tests/e2e/visual/docs/layout-invariants.meiva.okf.md
def _open_scripted_stream(
    playwright_browser: Browser, width: int, height: int, base_url: str
) -> tuple[BrowserContext, Page]:
    ctx = playwright_browser.new_context(viewport={"width": width, "height": height})
    page = open_page(ctx, base_url, "/", stub_api=True, scripted_sse=True)
    page.wait_for_function("() => window.__meivaSse && window.__meivaSse.ready()", timeout=15_000)
    delivered = page.evaluate("event => window.__meivaSse.emit(event)", timeline_snapshot())
    assert delivered == 1, f"the snapshot reached {delivered} handlers, not 1"
    expect(page.locator('[data-testid="timeline-surface"]')).to_contain_text(
        "Follow-up message 32", timeout=15_000
    )
    return ctx, page


def _stream_a_turn(page: Page) -> None:
    started = page.evaluate(
        "event => window.__meivaSse.emit(event)",
        {"role": "chat_start", "agent_id": 1, "item_id": "1.34", "content": "turn start"},
    )
    chunks = [
        {"role": "chat_delta", "agent_id": 1, "item_id": "1.34", "content": f"chunk {i} "}
        for i in range(STREAM_CHUNKS)
    ]
    delivered = page.evaluate("events => window.__meivaSse.emit(events)", chunks)
    assert started == 1 and delivered == STREAM_CHUNKS, (
        f"the stream took {started} start event(s) and {delivered} of {STREAM_CHUNKS} chunks — "
        "the app is not reading this stream, so the geometry below proves nothing"
    )
    expect(page.locator('[data-testid="timeline-surface"]')).to_contain_text(
        f"chunk {STREAM_CHUNKS - 1} ", timeout=15_000
    )


@pytest.mark.parametrize("width,height", STREAM_VIEWPORTS)
def test_a_streaming_turn_leaves_the_bottom_rule_still(
    width: int, height: int, playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_scripted_stream(playwright_browser, width, height, frontend_target)
    try:
        before = _measure_bottom_rule(page)
        _stream_a_turn(page)
        after = _measure_bottom_rule(page)
        for key in ("band", "footer"):
            assert abs(after[key]["y"] - before[key]["y"]) <= 1, (
                f"the {key} rule moved {after[key]['y'] - before[key]['y']:.2f}px while "
                f"{STREAM_CHUNKS} chunks arrived at {width}px"
            )
            assert abs(after[key]["height"] - before[key]["height"]) <= 1, (
                f"the {key} band's height changed by {after[key]['height'] - before[key]['height']:.2f}px "
                f"while {STREAM_CHUNKS} chunks arrived at {width}px"
            )
    finally:
        ctx.close()


@pytest.mark.parametrize("width,height", STREAM_VIEWPORTS)
def test_a_grown_composer_holds_the_sidebar_rule_while_the_turn_streams(
    width: int, height: int, playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_scripted_stream(playwright_browser, width, height, frontend_target)
    try:
        resting = _measure_bottom_rule(page)
        page.locator('[data-testid="composer-input"]').fill("draft line\n" * DRAFT_LINES)
        page.wait_for_function(
            "() => document.querySelector('[data-testid=\"composer-band\"]')"
            ".getBoundingClientRect().height > 60",
            timeout=5_000,
        )
        grown = _measure_bottom_rule(page)
        assert grown["band"]["height"] > resting["band"]["height"] + 20, (
            "the draft did not grow the composer band, so the grown-state check proves nothing"
        )
        assert abs(grown["footer"]["y"] - resting["footer"]["y"]) <= 1, (
            f"the sidebar's rule followed the composer's: it was y={resting['footer']['y']:.2f} at "
            f"rest and y={grown['footer']['y']:.2f} under a grown composer, while the band rose to "
            f"y={grown['band']['y']:.2f}"
        )
        _stream_a_turn(page)
        after = _measure_bottom_rule(page)
        assert abs(after["band"]["y"] - grown["band"]["y"]) <= 1
        assert abs(after["band"]["height"] - grown["band"]["height"]) <= 1, (
            f"the composer band collapsed from {grown['band']['height']:.2f}px to "
            f"{after['band']['height']:.2f}px while the turn streamed"
        )
        assert abs(after["footer"]["y"] - resting["footer"]["y"]) <= 1, (
            f"the sidebar's rule moved {after['footer']['y'] - resting['footer']['y']:.2f}px while "
            f"the turn streamed under a grown composer at {width}px"
        )
    finally:
        ctx.close()


@pytest.mark.parametrize("width,height", [(390, 844), (1440, 900)])
def test_shell_icon_size_is_uniform(
    width: int, height: int, playwright_browser: Browser, frontend_target: str
) -> None:
    ctx, page = _open_stubbed(playwright_browser, width, height, frontend_target)
    try:
        rows = [
            '[data-testid="timeline-surface"] header',
            "aside header",
            '[data-testid="sidebar-footer"]',
            '[data-testid="composer-band"]',
        ]
        checked: list[tuple[str, IconBox]] = []
        for selector in rows:
            row = page.locator(selector).first
            if row.count() == 0 or not row.is_visible():
                continue
            boxes = cast(
                "list[IconBox]",
                row.locator("svg").evaluate_all(
                    "els => els.map(el => { const b = el.getBoundingClientRect();"
                    " return {w: +b.width.toFixed(2), h: +b.height.toFixed(2),"
                    " label: (el.closest('button')?.getAttribute('aria-label')"
                    " || el.getAttribute('class') || '')}; })"
                ),
            )
            checked.extend((selector, box) for box in boxes if box["w"] and box["h"])
        assert len(checked) >= 3, (
            f"only {len(checked)} shell icons were measurable at {width}px: "
            f"{[selector for selector, _ in checked]}"
        )
        for selector, box in checked:
            assert abs(box["w"] - SHELL_ICON_PX) <= 0.5 and abs(box["h"] - SHELL_ICON_PX) <= 0.5, (
                f"{selector}: {str(box['label'])[:40]} is {box['w']}x{box['h']}px at {width}px, "
                f"expected {SHELL_ICON_PX}x{SHELL_ICON_PX} (the composer's upload glyph size)"
            )
    finally:
        ctx.close()
