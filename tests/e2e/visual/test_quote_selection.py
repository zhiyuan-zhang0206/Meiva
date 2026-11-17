# meiva:note
# Exercise real selection and draft behavior against the stubbed Home API boundary.

from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e._ports import FRONTEND_URL
from tests.e2e.visual.stubbed_home import new_context, open_page, wait_layout_settled


@pytest.fixture(scope="session")
def quote_frontend(request: pytest.FixtureRequest) -> str:
    request.getfixturevalue("frontend_proc")
    return FRONTEND_URL


@pytest.mark.parametrize("width", [1280, 390])
def test_selected_quote_preserves_draft_and_native_selection(
    playwright_browser: Browser,
    quote_frontend: str,
    width: int,
    tmp_path: Path,
) -> None:
    sent_text = (
        "First paragraph of the saved quote.\n\n"
        + "LongUnbrokenText" * 100
        + "\n\nFinal paragraph remains visible by default."
    )
    items = [
        {
            "item_id": "1.0",
            "kind": "agent_chat",
            "payload": "An exact selected phrase for discussion.",
            "source": "agent",
            "created_at": "2026-11-17T00:00:00Z",
            "show_timestamp": True,
            "user_facing": True,
            "source_message_id": "durable-quote-source",
            "source_block_idx": 0,
        },
        {
            "item_id": "2.0",
            "kind": "inbound_chat",
            "payload": "Another source.",
            "source": "user",
            "created_at": "2026-11-17T00:00:01Z",
            "show_timestamp": True,
            "inbound_id": 2,
            "source_inbound_id": 2,
            "quote": {
                "source_agent_id": 1,
                "source_item_id": "1.0",
                "source_message_id": "durable-quote-source",
                "source_block_idx": 0,
                "author": "Agent #1",
                "text": sent_text,
                "created_at": "2026-11-17T00:00:00Z",
            },
        },
    ]
    items.extend(
        {
            "item_id": f"{index}.0",
            "kind": "agent_chat",
            "payload": ("Older reading context remains stable. " * 20),
            "source": "agent",
            "created_at": "2026-11-17T00:00:02Z",
            "show_timestamp": True,
            "user_facing": True,
            "source_message_id": f"filler-{index}",
            "source_block_idx": 0,
        }
        for index in range(3, 28)
    )
    context = new_context(playwright_browser, width)
    try:
        page = open_page(
            context, quote_frontend, "/?agent_id=1", stub_api=True, timeline_items=items
        )
        wait_layout_settled(page)
        _check_sent_quote(page, sent_text, width, tmp_path)
        composer = page.locator("textarea").last
        composer.fill("Why did this happen?")
        expect(page.get_by_role("button", name="Quote selection", exact=True)).to_have_count(0)
        body = page.locator('[data-item-id="1.0"] [data-quote-body]')
        body.scroll_into_view_if_needed()
        # meiva:note
        # A real mouse double-click selects one native word without installing a DOM range.
        word = body.get_by_text("An exact selected phrase for discussion.", exact=True)
        word.dblclick(position={"x": 55, "y": 10})
        quote_button = page.get_by_role("button", name="Quote selection", exact=True)
        expect(quote_button).to_be_visible()
        page.keyboard.press("Shift+ArrowRight")
        expect(quote_button).to_be_visible()
        selected = page.evaluate("getSelection().toString()")
        assert selected.strip()
        viewport = page.locator('[data-slot="scroll-area-viewport"]:has([role="log"])')
        before_quote = viewport.evaluate("node => node.scrollTop")
        _ = page.screenshot(path=str(tmp_path / f"quote-selection-{width}.png"))
        quote_button.click()
        expect(composer).to_have_value("Why did this happen?")
        expect(page.get_by_role("button", name="Remove quote")).to_be_visible()
        expect(page.get_by_test_id("composer-quote")).to_contain_text(selected)
        _ = page.screenshot(path=str(tmp_path / f"quote-composer-{width}.png"))
        assert abs(viewport.evaluate("node => node.scrollTop") - before_quote) <= 1
        before_clear = viewport.evaluate("node => node.scrollTop")
        page.get_by_role("button", name="Remove quote").click()
        expect(composer).to_have_value("Why did this happen?")
        assert abs(viewport.evaluate("node => node.scrollTop") - before_clear) <= 1
        word.dblclick(position={"x": 55, "y": 10})
        expect(quote_button).to_be_visible()
        selected = page.evaluate("getSelection().toString()")
        quote_button.click()
        expect(page.get_by_test_id("composer-quote")).to_be_visible()
        expect(page.get_by_test_id("composer-quote")).to_contain_text(selected)
        page.reload()
        expect(composer).to_have_value("Why did this happen?")
        expect(page.get_by_role("button", name="Remove quote")).to_be_visible()
        # meiva:note
        # Keyboard selection in the composer must never create a chat quote entry.
        composer.focus()
        composer.press("ControlOrMeta+A")
        expect(quote_button).to_have_count(0)
        _send_quote(page, selected)
    finally:
        context.close()


def _send_quote(page: Page, selected: str) -> None:
    page.route(
        "**/api/agents/1/messages",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"inbound_id":9}'
        ),
    )
    with page.expect_request("**/api/agents/1/messages") as request:
        page.get_by_test_id("composer-send").click()
    payload = request.value.post_data_json
    assert payload is not None
    assert payload["content"] == "Why did this happen?"
    assert payload["quote"]["text"] == selected
    assert payload["quote"]["source_message_id"] == "durable-quote-source"
    expect(page.locator("textarea").last).to_have_value("")


@pytest.mark.parametrize(
    ("source", "payload", "body_text"),
    [
        (
            "exec_timeout",
            "Timeout selected fragment remains exact.",
            "Timeout selected fragment remains exact.",
        ),
        (
            None,
            "error:Timeout selected fragment remains exact.",
            "[error] Timeout selected fragment remains exact.",
        ),
    ],
)
def test_error_marker_body_can_be_quoted(
    playwright_browser: Browser,
    quote_frontend: str,
    source: str | None,
    payload: str,
    body_text: str,
) -> None:
    items = [
        {
            "item_id": "1.0",
            "kind": "system_marker",
            "source": source,
            "payload": payload,
            "created_at": "2026-11-17T00:00:00Z",
            "show_timestamp": True,
            "source_message_id": "durable-error-marker",
            "source_block_idx": 0,
        }
    ]
    context = new_context(playwright_browser, 1280)
    try:
        page = open_page(
            context, quote_frontend, "/?agent_id=1", stub_api=True, timeline_items=items
        )
        wait_layout_settled(page)
        page.get_by_test_id("turn-toggle").click()
        marker = page.locator('.timeline-item[data-item-id="1.0"]')
        toggle = marker.get_by_test_id("card-toggle")
        if toggle.count() and toggle.get_attribute("data-expanded") == "false":
            toggle.click()
        body = marker.locator("[data-quote-body]")
        expect(body).to_be_visible()
        composer = page.locator("textarea").last
        composer.fill("Why did this timeout happen?")
        body.get_by_text(body_text, exact=True).dblclick(position={"x": 80, "y": 8})
        quote_button = page.get_by_role("button", name="Quote selection", exact=True)
        expect(quote_button).to_be_visible()
        selected: object = page.evaluate("getSelection().toString()")
        assert isinstance(selected, str) and selected.strip() and selected != payload
        quote_button.click()
        expect(page.get_by_test_id("composer-quote")).to_contain_text(selected)
        expect(composer).to_have_value("Why did this timeout happen?")
    finally:
        context.close()


def _check_sent_quote(page: Page, text: str, width: int, tmp_path: Path) -> None:
    card = page.locator('[data-item-id="2.0"] [data-quote-exclude]')
    body = card.locator("p")
    expect(body).to_have_text(text)
    expect(card.locator("details, summary")).to_have_count(0)
    body.scroll_into_view_if_needed()
    geometry = body.evaluate("""node => {
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return {whiteSpace: style.whiteSpace, clamp: style.webkitLineClamp,
                height: rect.height, lineHeight: parseFloat(style.lineHeight),
                width: node.clientWidth, scrollWidth: node.scrollWidth,
                clientHeight: node.clientHeight, scrollHeight: node.scrollHeight};
    }""")
    assert geometry["whiteSpace"] == "pre-wrap"
    assert geometry["clamp"] == "none"
    assert geometry["height"] > geometry["lineHeight"] * 5
    assert geometry["scrollHeight"] <= geometry["clientHeight"] + 1
    assert geometry["scrollWidth"] <= geometry["width"] + 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(card.get_by_role("button", name="Quoted · Agent #1")).to_be_visible()
    _ = page.screenshot(path=str(tmp_path / f"quote-sent-full-{width}.png"))
    card.get_by_role("button", name="Quoted · Agent #1").click()
    assert (
        page.locator('.timeline-item[data-item-id="1.0"]').evaluate(
            "node => node.getAnimations().length"
        )
        > 0
    )
