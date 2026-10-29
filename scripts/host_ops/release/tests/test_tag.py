from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.host_ops.release import tag as release_tag


# meiva:note
# Fixed git invocations on a temporary repository; the arguments are test literals, never caller input.
def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603
        ("git", "-C", str(repo), *arguments), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "a").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    return repo, _git(repo, "rev-parse", "HEAD")


# meiva:note
# These are real tags in a real repository, not assertions about the strings a workflow carries:
# the case that was broken is invisible to anything that does not ask git.
def test_no_tag_means_publish(tmp_path: Path) -> None:
    repo, head = _repository(tmp_path)

    assert release_tag.tagged_commit(repo, "1.0.0") is None
    assert release_tag.plan(None, head) == "create"


def test_an_annotated_tag_on_the_certified_commit_is_a_no_op(tmp_path: Path) -> None:
    repo, head = _repository(tmp_path)
    _git(repo, "tag", "-a", "v1.0.0", head, "-m", "certified by the release gate")

    # meiva:note
    # Why the peel exists: the raw ref answers with the tag object, so a comparison without
    # ^{commit} can never match the commit it marks.
    assert _git(repo, "rev-parse", "refs/tags/v1.0.0") != head
    assert release_tag.tagged_commit(repo, "1.0.0") == head
    assert release_tag.plan(head, head) == "skip"


def test_a_tag_on_another_commit_refuses(tmp_path: Path) -> None:
    repo, first = _repository(tmp_path)
    _git(repo, "tag", "-a", "v1.0.0", first, "-m", "certified by the release gate")
    (repo / "b").write_text("b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "two")
    second = _git(repo, "rev-parse", "HEAD")

    assert release_tag.tagged_commit(repo, "1.0.0") == first
    assert release_tag.plan(first, second) == "refuse"


def test_a_lightweight_tag_peels_to_its_commit_too(tmp_path: Path) -> None:
    repo, head = _repository(tmp_path)
    _git(repo, "tag", "v1.0.0", head)

    assert release_tag.tagged_commit(repo, "1.0.0") == head
    assert release_tag.plan(head, head) == "skip"
