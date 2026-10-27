# meiva:note
# base.packages.docs.notes — the note model, tested as pure functions.

from __future__ import annotations

from base.packages.docs.frontmatter import parse_frontmatter_typed
from base.packages.docs.notes import (
    normalize_tags,
    parse_note,
)

# ── parse_frontmatter_typed ──


class TestParseFrontmatterTyped:
    def test_parses_and_preserves_types(self) -> None:
        parsed = parse_frontmatter_typed("---\ntitle: X\ntags: [a, b]\ncount: 3\n---\n\nbody\n")
        assert parsed is not None
        fm, body = parsed
        assert fm == {"title": "X", "tags": ["a", "b"], "count": 3}  # tags stays a list
        assert body == "\nbody\n"

    def test_no_frontmatter_returns_none(self) -> None:
        assert parse_frontmatter_typed("# just a heading\n") is None

    def test_unterminated_returns_none(self) -> None:
        assert parse_frontmatter_typed("---\ntitle: X\n") is None

    def test_bad_yaml_returns_none(self) -> None:
        assert parse_frontmatter_typed("---\ntitle: [unclosed\n---\nb\n") is None

    def test_non_dict_returns_none(self) -> None:
        assert parse_frontmatter_typed("---\n- just\n- a list\n---\nb\n") is None

    def test_closing_fence_at_end_of_file(self) -> None:
        parsed = parse_frontmatter_typed("---\ntitle: X\n---")
        assert parsed == ({"title": "X"}, "")

    def test_crlf_and_bom_tolerated(self) -> None:
        parsed = parse_frontmatter_typed("\ufeff---\r\ntitle: X\r\n---\r\n\r\nbody\r\n")
        assert parsed is not None
        fm, body = parsed
        assert fm == {"title": "X"}
        assert body == "\nbody\n"


# ── normalize_tags ──


class TestNormalizeTags:
    def test_string_is_wrapped(self) -> None:
        assert normalize_tags("tech-ops") == ("tech-ops",)

    def test_list_keeps_only_strings(self) -> None:
        assert normalize_tags(["a", "b"]) == ("a", "b")
        assert normalize_tags(["a", 5, "b", None]) == ("a", "b")

    def test_other_types_are_empty(self) -> None:
        assert normalize_tags(5) == ()
        assert normalize_tags({"a": 1}) == ()
        assert normalize_tags(None) == ()
        assert normalize_tags([]) == ()


# ── parse_note ──


class TestParseNote:
    def test_maps_all_fields(self) -> None:
        note = parse_note(
            "---\ntitle: Alpha\ndescription: A note\ntags: [meiva-internal]\n"
            "timestamp: 2026-01-02 03:04:05\nmeiva_agent: '1609'\nmeiva_machine: gateway-host\n"
            "---\n\n# Alpha\n",
            "health/alpha",
        )
        assert note is not None
        assert note.rel == "health/alpha"
        assert note.title == "Alpha"
        assert note.description == "A note"
        assert note.tags == ("meiva-internal",)
        assert note.timestamp is not None  # datetime → str()
        assert note.meiva_agent == "1609"
        assert note.meiva_machine == "gateway-host"
        assert note.body == "\n# Alpha\n"

    def test_missing_optional_fields_are_none(self) -> None:
        note = parse_note("---\ntitle: Beta\n---\n\n# Beta\n", "beta")
        assert note is not None
        assert note.title == "Beta"
        assert note.description is None
        assert note.tags == ()
        assert note.timestamp is None
        assert note.meiva_agent is None
        assert note.meiva_machine is None

    def test_title_falls_back_to_rel(self) -> None:
        note = parse_note("---\ntags: [x]\n---\n\nb\n", "sub/note")
        assert note is not None
        assert note.title == "sub/note"

    def test_non_string_title_and_description_coerced(self) -> None:
        note = parse_note("---\ntitle: 42\ndescription: 7\n---\n\nb\n", "n")
        assert note is not None
        assert note.title == "42"
        assert note.description == "7"

    def test_non_note_returns_none(self) -> None:
        assert parse_note("# no frontmatter\n", "plain") is None
        assert parse_note("---\ntags: [unclosed\n---\nb\n", "bad") is None
