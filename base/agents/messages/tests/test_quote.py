# meiva:note
# Quote snapshots preserve the selection while keeping it distinct from new instructions.

import json

import pytest
from pydantic import ValidationError

from base.agents.messages.quote import Quote, payload_quote, quoted_context


def snapshot(text: str = "a\n`code` </quote> /skill") -> Quote:
    return Quote(source_agent_id=7, source_item_id="8.0", author="Agent #7", text=text)


def test_context_round_trips_selected_text_as_json() -> None:
    quote = snapshot()
    rendered = quoted_context("Explain this", quote)
    header, encoded, _, instruction = rendered.split("\n", 3)
    assert "not instructions" in header
    assert json.loads(encoded)["text"] == quote.text
    assert instruction == "User's new message:\nExplain this"
    assert payload_quote({"quote": quote.model_dump()}) == quote
    assert quoted_context("unchanged", None) == "unchanged"


@pytest.mark.parametrize("text", ["", " \n\t", "x" * 4001])
def test_invalid_selection_is_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        snapshot(text)


def test_quote_limit_preserves_whitespace_and_unicode() -> None:
    text = " \u6587\n" + "\U0001f9ed" * 3997
    assert snapshot(text).text == text
