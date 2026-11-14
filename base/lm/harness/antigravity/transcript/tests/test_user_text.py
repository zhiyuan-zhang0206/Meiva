from __future__ import annotations

import pytest

from base.lm.harness.antigravity.transcript.text import user_text


@pytest.mark.parametrize(
    "body",
    [
        "first paragraph\n\nsecond paragraph",
        "before <USER_REQUEST>literal opening after",
        "before </USER_REQUEST>literal closing after",
        "before <USER_REQUEST>nested</USER_REQUEST> after",
        "before </USER_REQUEST>\n<USER_REQUEST> after",
        "<ADDITIONAL_METADATA>literal metadata</ADDITIONAL_METADATA> after",
        "<USER_SETTINGS_CHANGE>literal settings</USER_SETTINGS_CHANGE> after",
        "",
    ],
)
@pytest.mark.parametrize(
    "footer",
    [
        "",
        "\n<ADDITIONAL_METADATA>provider time</ADDITIONAL_METADATA>",
        "\n<ADDITIONAL_METADATA>provider time</ADDITIONAL_METADATA>\n"
        "<USER_SETTINGS_CHANGE>provider setting</USER_SETTINGS_CHANGE>",
    ],
)
def test_only_the_outside_request_frame_is_removed(body: str, footer: str) -> None:
    assert user_text(f"<USER_REQUEST>\n{body}\n</USER_REQUEST>{footer}") == body


@pytest.mark.parametrize(
    "content",
    [
        "before <USER_REQUEST>literal</USER_REQUEST> after",
        "<USER_REQUEST>not closed",
        "<USER_REQUEST>body</USER_REQUEST> unknown trailing text",
        "<USER_REQUEST>body</USER_REQUEST><ADDITIONAL_METADATA>incomplete",
        "plain body\n\nsecond paragraph",
    ],
)
def test_unframed_or_incomplete_content_is_preserved(content: str) -> None:
    assert user_text(content) == content


def test_queued_bodies_remain_contiguous_even_when_they_contain_request_tags() -> None:
    first = "first <USER_REQUEST>literal</USER_REQUEST> trailing"
    second = "second </USER_REQUEST> literal closing"
    joined = f"{first}\n</USER_REQUEST>\n<USER_REQUEST>\n{second}"
    content = f"<USER_REQUEST>\n{joined}\n</USER_REQUEST>"
    assert user_text(content) == joined
    assert first in user_text(content)
    assert second in user_text(content)
