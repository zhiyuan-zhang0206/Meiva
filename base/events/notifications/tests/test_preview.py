from __future__ import annotations

from base.events.notifications.preview import PREVIEW_LIMIT, unread_preview


def test_the_preview_drops_the_visibility_prefix_and_markdown_noise() -> None:
    text = "## Done\n\n- **Built** the [report](https://x.test/r)\n- `ok`"
    assert unread_preview(text) == "Done Built the report ok"


def test_the_preview_is_cut_at_the_limit_with_an_ellipsis() -> None:
    preview = unread_preview("word " * 100)
    assert len(preview) == PREVIEW_LIMIT
    assert preview.endswith("…")
    assert unread_preview("short") == "short"
