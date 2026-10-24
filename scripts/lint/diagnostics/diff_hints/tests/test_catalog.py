from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.lint.diagnostics.diff_hints.catalog import spec_of
from scripts.lint.diagnostics.diff_hints.model import Ack, HintId, Site
from scripts.lint.diagnostics.diff_hints.render import render_open, render_summary

ROOT = Path(__file__).resolve().parents[5]
DOC = ROOT / "scripts/lint/docs/diff-hints/catalog.meiva.okf.md"
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


# meiva:note
# GitHub's anchor rule: lowercase, drop punctuation except hyphen, underscore and space.
def slug(heading: str) -> str:
    lowered = heading.lower().replace("`", "")
    return re.sub(r"[^\w\- ]", "", lowered).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else HEADING.match(line)
        if match:
            found.add(slug(match.group(1)))
    return found


@pytest.mark.parametrize("hint", list(HintId))
def test_every_hint_has_a_principle_a_recipe_and_a_rule_that_exists(hint: HintId) -> None:
    spec = spec_of(hint)
    assert spec.why.endswith(".") and len(spec.why) > 40
    assert "1)" in spec.instead
    path, _, anchor = spec.see.partition("#")
    target = ROOT / path
    assert target.is_file(), spec.see
    assert anchor in anchors(target), f"{spec.see}: no such heading"


@pytest.mark.parametrize("hint", list(HintId))
def test_every_hint_is_a_row_of_the_catalog_table_in_the_documentation(hint: HintId) -> None:
    text = DOC.read_text(encoding="utf-8")
    assert f"| `{hint.value}` |" in text


def test_the_first_site_of_an_id_prints_the_whole_text_and_later_sites_only_the_line() -> None:
    sites = [
        Site(
            HintId.SERVICE_LOCATOR, "cli/a.py", 3, "`app.state` fetches inputs from the application"
        ),
        Site(
            HintId.SERVICE_LOCATOR, "cli/b.py", 9, "`app.state` fetches inputs from the application"
        ),
        Site(
            HintId.SPY_ASSERT,
            "cli/tests/test_c.py",
            4,
            "`.call_count` asserts how a mock was called",
        ),
    ]
    text = render_open(sites)
    assert text.count("why:") == 2
    assert text.count("instead:") == 2
    assert "cli/b.py:9: hint service-locator" in text
    assert "Hint-Ack: service-locator cli/a.py:3 -- <reason>" in text
    assert "Hint-Ack: service-locator cli/a.py -- <reason>" not in text


def test_a_noisy_id_is_capped_and_the_cap_is_said() -> None:
    sites = [
        Site(HintId.DELETED_TEST, f"cli/tests/test_{i}.py", 1, "1 test(s) removed: t")
        for i in range(12)
    ]
    text = render_open(sites, limit=3)
    assert "... 9 more `deleted-test` site(s) in 9 file(s)" in text
    assert text.count("hint deleted-test") == 3
    templates = [line for line in text.splitlines() if line.strip().startswith("Hint-Ack:")]
    assert len(templates) <= 7
    assert any("Hint-Ack: deleted-test cli/tests/ -- <reason>" in line for line in templates)


def test_the_summary_marks_a_directory_answer_and_a_wide_answer_as_broad() -> None:
    narrow = Ack(HintId.SERVICE_LOCATOR, "cli/x.py", 4, "composition root of the daemon")
    directory = Ack(HintId.DELETED_TEST, "cli/tests/", None, "the suite moved into the package")
    wide = Ack(HintId.TEST_SEAM, "cli/tests/test_a.py", None, "polls | real child")
    text = render_summary(
        [("abc123def", narrow, 1), ("abc123def", directory, 7), ("1234abcd", wide, 12)], 2
    )
    assert "3 answer(s) on this branch; 2 site(s) still open." in text
    assert "| `abc123def` | `service-locator` | `cli/x.py:4` | 1 |" in text
    assert "`cli/tests/` **broad**" in text
    assert "`cli/tests/test_a.py` **broad**" in text
    assert "polls \\| real child" in text
    assert "`cli/x.py:4` **broad**" not in text


def test_a_summary_with_no_answers_still_says_what_is_open() -> None:
    assert render_summary([], 0).endswith("0 answer(s) on this branch; 0 site(s) still open.")
