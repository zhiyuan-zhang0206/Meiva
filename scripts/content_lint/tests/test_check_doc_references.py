from __future__ import annotations

from pathlib import Path

import pytest

from base.host.proc import run_bounded
from scripts.content_lint import check_doc_references
from scripts.content_lint.check_doc_references import REPO, check_doc

_NO_COMMANDS: dict[tuple[str, ...], set[str]] = {}
_TEST_NODES = frozenset({"pkg/docs/real.meiva.okf.md"})


def _targets(tmp_path: Path, body: str, *, nodes: frozenset[str] | None = None) -> list[str]:
    doc = tmp_path / "doc.md"
    doc.write_text(body)
    return [message for _lineno, message in check_doc(doc, _NO_COMMANDS, nodes=nodes)]


def test_prose_link_to_a_missing_file_is_reported(tmp_path: Path) -> None:
    assert _targets(tmp_path, "See [the note](missing.md).\n") == ["`missing.md` — no such file"]


def test_link_to_an_existing_file_is_not_reported(tmp_path: Path) -> None:
    (tmp_path / "there.md").write_text("hi")
    assert _targets(tmp_path, "See [the note](there.md).\n") == []


@pytest.mark.parametrize(
    ("shape", "body"),
    [
        ("fenced", "Format:\n\n```\n- [Title](path.md) — what it holds\n```\n"),
        ("indented", "Format:\n\n    - [Title](path.md) — what it holds\n"),
        ("inline", "Write `[Title](path.md)` on its own line.\n"),
    ],
)
def test_a_sample_path_is_not_a_link(shape: str, body: str, tmp_path: Path) -> None:
    assert _targets(tmp_path, body) == [], f"{shape} sample was read as a link"


def test_an_indented_block_stays_open_across_blank_lines(tmp_path: Path) -> None:
    body = "Format:\n\n    - [One](a.md)\n\n    - [Two](b.md)\n"
    assert _targets(tmp_path, body) == []


def test_a_nested_list_item_is_still_checked(tmp_path: Path) -> None:
    body = "- outer\n\n    - nested [the note](missing.md)\n"
    assert _targets(tmp_path, body) == ["`missing.md` — no such file"]


def test_a_link_after_an_indented_block_is_checked_again(tmp_path: Path) -> None:
    body = "Format:\n\n    - [Title](path.md)\n\nThen see [the note](missing.md).\n"
    assert _targets(tmp_path, body) == ["`missing.md` — no such file"]


def test_the_memory_template_is_clean() -> None:
    template = REPO / "meiva_builtins" / "plugins" / "meiva_memory" / "template" / "MEMORY.md"
    assert check_doc(template, _NO_COMMANDS) == []


def test_a_docs_future_style_moved_link_still_fails(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("See [the plan](moved.md) for detail.\n")
    problems = check_doc(doc, _NO_COMMANDS, skip_flags=True, allow_planned=True)
    assert [m for _lineno, m in problems] == ["`moved.md` — no such file"]


def test_the_planned_marker_exempts_the_link_it_follows(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("See [the future module](ops/identity.py) (planned) for detail.\n")
    problems = check_doc(doc, _NO_COMMANDS, skip_flags=True, allow_planned=True)
    assert problems == []


def test_the_planned_marker_is_ignored_outside_docs_future(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("See [the future module](ops/identity.py) (planned) for detail.\n")
    problems = check_doc(doc, _NO_COMMANDS)
    assert [m for _lineno, m in problems] == ["`ops/identity.py` — no such file"]


def test_the_planned_marker_must_be_immediately_adjacent(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("See [one](missing-one.md) and [two](missing-two.md) (planned).\n")
    problems = check_doc(doc, _NO_COMMANDS, skip_flags=True, allow_planned=True)
    assert [m for _lineno, m in problems] == ["`missing-one.md` — no such file"]


@pytest.mark.parametrize(
    "link",
    [
        "[[gone.meiva.okf.md]]",
        "[[gone.meiva.okf.md|the label]]",
        "[[../../gone.meiva.okf.md|group]]",
    ],
)
def test_a_dangling_wikilink_is_reported_with_or_without_a_label(link: str, tmp_path: Path) -> None:
    target = link[2:-2].split("|", maxsplit=1)[0]
    assert _targets(tmp_path, f"See {link} for detail.\n", nodes=_TEST_NODES) == [
        f"`[[{target}]]` — no such OKF node"
    ]


@pytest.mark.parametrize("link", ["[[real.meiva.okf.md]]", "[[real.meiva.okf.md|the label]]"])
def test_a_resolving_wikilink_is_clean(link: str, tmp_path: Path) -> None:
    assert _targets(tmp_path, f"See {link} for detail.\n", nodes=_TEST_NODES) == []


@pytest.mark.parametrize(
    "body",
    [
        "Write `[[gone.meiva.okf.md|label]]` for an edge.\n",
        "```\n[[gone.meiva.okf.md|label]]\n```\n",
        "Format:\n\n    [[gone.meiva.okf.md|label]]\n",
    ],
)
def test_a_wikilink_sample_is_not_a_link(body: str, tmp_path: Path) -> None:
    assert _targets(tmp_path, body, nodes=_TEST_NODES) == []


def test_the_planned_marker_exempts_a_wikilink_in_a_plan(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("Next: [[later.meiva.okf.md|the node]] (planned), then [[gone.meiva.okf.md]].\n")
    problems = check_doc(doc, _NO_COMMANDS, skip_flags=True, allow_planned=True, nodes=_TEST_NODES)
    assert [m for _lineno, m in problems] == ["`[[gone.meiva.okf.md]]` — no such OKF node"]


def test_skip_flags_suppresses_an_invalid_flag(tmp_path: Path) -> None:
    commands: dict[tuple[str, ...], set[str]] = {("meiva",): {"--verbose"}}
    doc = tmp_path / "doc.md"
    doc.write_text("Run `meiva --not-a-real-flag`.\n")
    assert check_doc(doc, commands) == [(1, "`meiva --not-a-real-flag` — no such flag")]
    assert check_doc(doc, commands, skip_flags=True) == []


def test_live_cli_options_reject_wrong_command(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Use `meiva stop --keep-infra` or `meiva agents timeline 7 --limit 8`.\n"
        "Wrong: `meiva start --keep-infra`.\n"
    )
    assert check_doc(doc, check_doc_references.meiva_flags()) == [
        (2, "`meiva start --keep-infra` — no such flag")
    ]


def _skill(tmp_path: Path, body: str, *, name: str = "SKILL.md") -> tuple[Path, list[str]]:
    skill_dir = tmp_path / ".agents" / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    doc = skill_dir / name
    doc.write_text(body)
    return doc, [m for _l, m in check_doc(doc, _NO_COMMANDS)]


def test_skill_backtick_ref_to_missing_file_is_reported(tmp_path: Path) -> None:
    _doc, problems = _skill(tmp_path, "See `references/gone.md` for the model.\n")
    assert problems == ["`references/gone.md` — no such file (skill references/)"]


def test_skill_backtick_ref_to_existing_file_is_clean(tmp_path: Path) -> None:
    (tmp_path / ".agents" / "skills" / "demo" / "references").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agents" / "skills" / "demo" / "references" / "there.md").write_text("hi")
    _doc, problems = _skill(tmp_path, "See `references/there.md`.\n")
    assert problems == []


def test_nested_skill_resolves_against_ancestor_library(tmp_path: Path) -> None:
    root = tmp_path / "meiva_builtins" / "skills" / "serious"
    (root / "references").mkdir(parents=True)
    (root / "references" / "book.md").write_text("hi")
    nested = root / "ai-era" / "child"
    nested.mkdir(parents=True)
    doc = nested / "SKILL.md"
    doc.write_text("See `references/book.md`.\n")
    problems = [m for _l, m in check_doc(doc, _NO_COMMANDS)]
    assert problems == []


def test_nested_skill_own_library_shadows_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "meiva_builtins" / "skills" / "serious"
    (root / "references").mkdir(parents=True)
    (root / "references" / "book.md").write_text("old")
    nested = root / "child"
    (nested / "references").mkdir(parents=True)
    (nested / "references" / "book.md").write_text("new")
    doc = nested / "SKILL.md"
    doc.write_text("See `references/book.md`.\n")
    problems = [m for _l, m in check_doc(doc, _NO_COMMANDS)]
    assert problems == []


def test_placeholder_skill_ref_is_skipped(tmp_path: Path) -> None:
    _doc, problems = _skill(tmp_path, "Write `references/<name>.md` for each.\n")
    assert problems == []


def test_non_skill_doc_backtick_ref_stays_unchecked(tmp_path: Path) -> None:
    _doc, problems = _skill(tmp_path, "See `references/gone.md`.\n", name="notes.md")
    assert problems == []


def test_skill_bare_skills_prefix_ref_is_reported(tmp_path: Path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text(".venv/bin/python skills/gmail/reference/feed.py search\n", encoding="utf-8")
    problems = check_doc(doc, {})
    assert any("no top-level skills/" in msg for _, msg in problems)


def test_skill_load_dir_prefix_ref_is_clean(tmp_path: Path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text(
        "$MEIVA_HOME/source/.venv/bin/python $MEIVA_HOME/skills/gmail/reference/feed.py search\n",
        encoding="utf-8",
    )
    assert check_doc(doc, {}) == []


def test_skill_prose_about_load_dir_is_clean(tmp_path: Path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text(
        "Scripts run from `$MEIVA_HOME/skills/<name>/` (the load dir).\n", encoding="utf-8"
    )
    assert check_doc(doc, {}) == []


@pytest.mark.parametrize("explicit_docs", [False, True])
def test_scan_uses_one_repository_for_docs_and_relative_wikilinks(
    tmp_path: Path, explicit_docs: bool
) -> None:
    for name, target_exists in (("present", True), ("absent", False)):
        repo = tmp_path / name
        guide = repo / "docs" / "area" / "guide.md"
        guide.parent.mkdir(parents=True)
        guide.write_text("See [[../target.meiva.okf.md]].\n")
        if target_exists:
            (repo / "docs" / "target.meiva.okf.md").write_text("Local node.\n")
            other = repo / "other" / "target.meiva.okf.md"
            other.parent.mkdir()
            other.write_text("A second basename forces relative resolution.\n")
        run_bounded(["git", "init", "-q", str(repo)], timeout=10).check_returncode()
        run_bounded(["git", "-C", str(repo), "add", "."], timeout=10).check_returncode()
        assert check_doc_references.main(
            repo=repo, commands={}, docs=[guide] if explicit_docs else None
        ) == (0 if target_exists else 1)


def test_skill_reference_cannot_escape_the_selected_repository(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    doc = repo / ".agents" / "skills" / "sample" / "SKILL.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("See `references/outside.md`.\n")
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "outside.md").write_text("Outside the checkout.\n")
    assert check_doc_references.check_skill_backtick_refs(doc, doc.read_text(), repo=repo) == [
        "references/outside.md"
    ]


@pytest.mark.parametrize("explicit_docs", [False, True])
def test_scan_preserves_checkout_and_skill_symlink_paths(
    tmp_path: Path, explicit_docs: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    actual = tmp_path / "actual"
    source = actual / "meiva_builtins" / "skills" / "sample" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("See `references/missing.md`.\n")
    mirror = actual / ".agents" / "skills" / "sample" / "SKILL.md"
    mirror.parent.mkdir(parents=True)
    mirror.symlink_to(source)
    guide = actual / "docs" / "guide.md"
    guide.parent.mkdir()
    guide.write_text("See [[node.meiva.okf.md]].\n")
    (guide.parent / "node.meiva.okf.md").write_text("Local node.\n")
    run_bounded(["git", "init", "-q", str(actual)], timeout=10).check_returncode()
    run_bounded(["git", "-C", str(actual), "add", "."], timeout=10).check_returncode()
    repo = tmp_path / "checkout-alias"
    repo.symlink_to(actual, target_is_directory=True)
    selected = [repo / "docs" / "guide.md", repo / mirror.relative_to(actual)]
    assert (
        check_doc_references.main(repo=repo, commands={}, docs=selected if explicit_docs else None)
        == 1
    )
    output = capsys.readouterr().out
    assert output == (
        ".agents/skills/sample/SKILL.md:1: "
        "`references/missing.md` — no such file (skill references/)\n"
    )
