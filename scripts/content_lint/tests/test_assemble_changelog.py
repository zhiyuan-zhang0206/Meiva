# meiva:link docs/conventions/changelog.md
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.content_lint import assemble_changelog as fold

_CLEAN = """# Changelog

Notable changes, newest first.

## [Unreleased]

### Added
- A newer addition
  ([owner](docs/owner.md)).

- An older addition.

### Changed
- A change.

### Fixed
- The first fix.

### Security
- A security note.
"""


def _repo(tmp_path: Path, changelog: str = _CLEAN) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (tmp_path / ".changes").mkdir()
    return tmp_path


def _fragment(root: Path, name: str, body: str) -> Path:
    path = root / ".changes" / name
    path.write_text(body, encoding="utf-8")
    return path


# meiva:note
# The Git calls run fixed argv against this test's own temporary directory.
def _git(root: Path, *args: str, at: str) -> None:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_DATE": at,
        "GIT_COMMITTER_DATE": at,
    }
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args], check=True, env=environment, capture_output=True
    )


def _git_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    _git(root, "init", "--quiet", at="2026-10-20T00:00:00+00:00")
    _git(root, "add", "-A", at="2026-10-20T00:00:00+00:00")
    _git(root, "commit", "--quiet", "-m", "base", at="2026-10-20T00:00:00+00:00")
    return root


def test_canonical_changelog_round_trips_byte_for_byte() -> None:
    preamble, sections, trailer = fold.parse_changelog(_CLEAN)
    assert fold.render_changelog(preamble, sections, trailer) == _CLEAN


def test_a_repeated_section_folds_into_one_block_keeping_document_order() -> None:
    # meiva:note
    # The hand-merge artefact: a second `### Added` appended by a later merge. Its
    # entries are older than the first block's, so keeping document order is the
    # same thing as keeping newest first.
    messy = _CLEAN.replace(
        "### Fixed\n- The first fix.\n",
        "### Fixed\n- The first fix.\n\n### Added\n- An older addition from a second block.\n",
    )
    preamble, sections, trailer = fold.parse_changelog(messy)
    rendered = fold.render_changelog(preamble, sections, trailer)
    assert rendered.count("### Added") == 1
    assert rendered.index("- A newer addition") < rendered.index("- An older addition.")
    assert rendered.index("- An older addition.") < rendered.index("- An older addition from")


def test_a_fragment_lands_on_top_of_its_section(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _fragment(root, "6566-timeline-chrome.md", "type: Added\n\n- The newest addition.\n")
    folded = fold.fold_fragments(_CLEAN, fold.read_fragments(root))
    assert "### Added\n- The newest addition.\n\n- A newer addition" in folded
    assert "The newest addition" not in _CLEAN


def test_fragments_order_by_the_commit_that_added_them_not_mtime(tmp_path: Path) -> None:
    # meiva:note
    # A clone, a rebase or a copy resets mtime; the merge order does not.
    root = _git_repo(tmp_path)
    older = _fragment(root, "1-first.md", "type: Added\n\n- The first one.\n")
    _git(root, "add", "-A", at="2026-10-20T10:00:00+00:00")
    _git(root, "commit", "--quiet", "-m", "first", at="2026-10-20T10:00:00+00:00")
    _fragment(root, "2-second.md", "type: Added\n\n- The second one.\n")
    _git(root, "add", "-A", at="2026-10-20T11:00:00+00:00")
    _git(root, "commit", "--quiet", "-m", "second", at="2026-10-20T11:00:00+00:00")
    newer = root / ".changes" / "2-second.md"
    os.utime(older, (0, 0))
    os.utime(newer, (0, 0))
    assert [f.path.name for f in fold.read_fragments(root)] == ["2-second.md", "1-first.md"]


def test_an_untracked_fragment_falls_back_to_its_mtime(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _fragment(root, "1-older.md", "type: Added\n\n- Older.\n")
    _fragment(root, "2-newer.md", "type: Added\n\n- Newer.\n")
    os.utime(root / ".changes" / "1-older.md", (1_000_000, 1_000_000))
    os.utime(root / ".changes" / "2-newer.md", (2_000_000, 2_000_000))
    assert [f.path.name for f in fold.read_fragments(root)] == ["2-newer.md", "1-older.md"]


@pytest.mark.parametrize(
    ("name", "body", "message"),
    [
        ("7-bad-name.txt", "type: Added\n\n- An entry.\n", "`<pr>-<slug>.md`"),
        (
            "7-unknown-type.md",
            "type: Improvement\n\n- An entry.\n",
            "unknown section 'Improvement'",
        ),
        ("7-no-blank-line.md", "type: Added\n- An entry.\n", "line 2 must be blank"),
        ("7-not-an-entry.md", "type: Added\n\nAn entry without a dash.\n", "must start with `- `"),
        ("7-empty-entry.md", "type: Added\n\n- \n", "the entry is empty"),
        ("7-trailing-blank.md", "type: Added\n\n- An entry.\n\n", "drop the blank line"),
        ("7-two-entries.md", "type: Added\n\n- First.\n\n- Second.\n", "one entry per fragment"),
        ("7-no-newline.md", "type: Added\n\n- An entry.", "must end with a newline"),
    ],
)
def test_check_reports_every_malformed_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, body: str, message: str
) -> None:
    root = _repo(tmp_path)
    _fragment(root, name, body)
    assert fold.main(["--check"], root=root) == 1
    assert message in capsys.readouterr().out


def test_check_accepts_a_clean_fragment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path)
    _fragment(
        root, "6566-timeline-chrome.md", "type: Fixed\n\n- An entry\n  ([doc](docs/owner.md)).\n"
    )
    assert fold.main(["--check"], root=root) == 0
    assert capsys.readouterr().out == ""


def test_default_mode_prints_the_diff_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _fragment(root, "6566-timeline-chrome.md", "type: Fixed\n\n- A dry-run fix.\n")
    assert fold.main([], root=root) == 0
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == _CLEAN
    assert "+- A dry-run fix." in capsys.readouterr().out


def test_write_folds_the_fragment_in_and_deletes_it(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    path = _fragment(root, "6566-timeline-chrome.md", "type: Fixed\n\n- A folded fix.\n")
    assert fold.main(["--write"], root=root) == 0
    assert not path.exists()
    assert "### Fixed\n- A folded fix.\n\n- The first fix." in (root / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )


def test_write_without_fragments_is_a_no_op(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert fold.main(["--write"], root=root) == 0
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == _CLEAN


def test_a_changelog_without_an_unreleased_section_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, "# Changelog\n\n### Added\n- An entry.\n")
    assert fold.main([], root=root) == 1
    assert "no `## [Unreleased]` line" in capsys.readouterr().err


def test_prose_inside_a_section_is_an_error() -> None:
    text = _CLEAN.replace(
        "### Fixed\n- The first fix.\n", "### Fixed\nA paragraph, not an entry.\n"
    )
    with pytest.raises(fold.ChangelogError, match="not an entry"):
        fold.parse_changelog(text)


def test_the_checkout_changelog_keeps_every_entry_and_folds_to_a_fixed_point() -> None:
    # meiva:note
    # The acceptance the fold owes the real file: same entries, same order, a
    # stable output that a second run does not move.
    text = (fold.REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    preamble, sections, trailer = fold.parse_changelog(text)
    folded = fold.render_changelog(preamble, sections, trailer)
    _, refolded_sections, _ = fold.parse_changelog(folded)
    assert refolded_sections == sections
    assert fold.render_changelog(*fold.parse_changelog(folded)) == folded
