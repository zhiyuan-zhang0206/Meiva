import json
from pathlib import Path

import pytest

from gateway.agents.history.timeline import TimelineResponse
from gateway.message_admission.cursor import item_sort_key, parse_cursor

_PAGES = Path(__file__).resolve().parents[4] / "ui/web/src/lib/timeline/tests/epoch-pages"
_FILES = sorted(_PAGES.glob("*.json"))


def test_the_fixture_pages_exist() -> None:
    assert len(_FILES) == 5


@pytest.mark.parametrize("path", _FILES, ids=lambda path: path.name)
def test_a_fixture_page_is_in_conversation_order_with_every_id_a_cursor(path: Path) -> None:
    page = TimelineResponse.model_validate(json.loads(path.read_text())["response"])
    ids = [item.item_id for item in page.items]
    assert ids == sorted(ids, key=item_sort_key)
    for item_id in ids:
        assert parse_cursor(item_id) is not None
