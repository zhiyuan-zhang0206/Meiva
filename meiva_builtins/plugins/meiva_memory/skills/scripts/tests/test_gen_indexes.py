# meiva:note
# `gen_indexes.py` is a script run as its own process: it resolves the Meiva home at its entry and
# writes the directory indexes of that home's pool.
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "gen_indexes.py"
NOTE = "---\ntitle: {title}\ndescription: {description}\n---\n"


def _note(title: str, description: str) -> str:
    return NOTE.format(title=title, description=description)


def _git(pool: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(pool), *args], check=True, capture_output=True)


def _generate(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(tmp_path)
    res = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stderr


def _generate_result(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_the_script_writes_the_directory_indexes_of_the_pool_under_its_home(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "memory"
    (pool / "topics").mkdir(parents=True)
    (pool / "topics" / "retry.md").write_text(
        _note("Retry rule", "retries above three hurt"), encoding="utf-8"
    )

    _generate(tmp_path)

    assert "* [Retry rule](retry.md) - retries above three hurt" in (
        pool / "topics" / "index.md"
    ).read_text(encoding="utf-8")
    assert "* [topics/](topics/) - 1 notes" in (pool / "index.md").read_text(encoding="utf-8")


# meiva:note
# The generator enumerates the pool's git index, not its working tree (2026-11-06). The pool
# validator counts `git ls-files` -- so an untracked draft must reach neither a count here nor a
# pointer line: a line pointing at a file that never reaches the commit is a broken index that
# nothing reports. A staged note must reach both, which makes the ordinary "write, add, generate,
# commit" flow independent of the order an author happens to use.
def test_an_untracked_draft_is_invisible_and_a_staged_note_is_counted(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    (pool / "topics").mkdir(parents=True)
    (pool / "topics" / "committed.md").write_text(
        _note("Committed rule", "already in the index"), encoding="utf-8"
    )
    _git(pool, "init", "-q")
    _git(pool, "add", "-A")
    _git(pool, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "base")
    (pool / "topics" / "draft.md").write_text(
        _note("Draft rule", "not staged yet"), encoding="utf-8"
    )

    _generate(tmp_path)

    index = (pool / "topics" / "index.md").read_text(encoding="utf-8")
    assert "draft.md" not in index
    assert "* [topics/](topics/) - 1 notes" in (pool / "index.md").read_text(encoding="utf-8")

    _git(pool, "add", "topics/draft.md")
    _generate(tmp_path)

    index = (pool / "topics" / "index.md").read_text(encoding="utf-8")
    assert "* [Draft rule](draft.md) - not staged yet" in index
    assert "* [topics/](topics/) - 2 notes" in (pool / "index.md").read_text(encoding="utf-8")


# meiva:note
# A pool that is a subdirectory of some larger checkout has an index whose paths start above it;
# enumerating that index would count the wrong files. Such a pool keeps the filesystem walk.
def test_a_pool_nested_in_another_checkout_walks_the_filesystem(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    pool = outer / "memory"
    (pool / "topics").mkdir(parents=True)
    (pool / "topics" / "nested.md").write_text(
        _note("Nested rule", "the pool is not the checkout root"), encoding="utf-8"
    )
    _git(outer, "init", "-q")
    _git(outer, "add", "-A")
    _git(outer, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "base")

    env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "memory").symlink_to(pool)

    res = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert res.returncode == 0, res.stderr
    assert "* [topics/](topics/) - 1 notes" in (pool / "index.md").read_text(encoding="utf-8")


# meiva:note
# The index cannot see a draft, and the run used to look identical either way (2026-11-07: 6111
# wrote eleven notes, ten reached the index, and neither `validate.py` nor `gen --check` said a
# word -- the missing note was never part of the bundle they inspect). The generator now names the
# skipped files on stderr. It still does not index them: an untracked draft stays invisible to the
# validator too, so the two sides keep agreeing.
def test_an_untracked_draft_is_named_on_stderr_and_never_indexed(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    (pool / "topics").mkdir(parents=True)
    (pool / "topics" / "draft.md").write_text(_note("Draft rule", "held back"), encoding="utf-8")
    (pool / "topics" / "kept.md").write_text(_note("Kept rule", "staged"), encoding="utf-8")
    _git(pool, "init", "-q")
    _git(pool, "add", "topics/kept.md")

    res = _generate_result(tmp_path)

    assert res.returncode == 0, res.stderr
    assert "1 untracked .md not indexed" in res.stderr
    assert "topics/draft.md" in res.stderr
    index = (pool / "topics" / "index.md").read_text(encoding="utf-8")
    assert "Draft rule" not in index

    _git(pool, "add", "topics/draft.md")
    res = _generate_result(tmp_path)

    assert "untracked .md not indexed" not in res.stderr
    assert "* [Draft rule](draft.md) - held back" in (pool / "topics" / "index.md").read_text(
        encoding="utf-8"
    )
