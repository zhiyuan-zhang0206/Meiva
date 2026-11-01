# meiva:note
# What the sub-check verdict cache promises: clean checkouts reuse passing runs across commit-message
# amends and non-Python doc edits, dirty checkouts and failing or unreadable verdicts never populate
# or read the cache, and the entry cap bounds the cache file.

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from scripts.hooks.diff_checks import (
    ChangeSet,
    Selection,
    check,
    open_cache_context,
    parse_args,
    plan,
    run_child,
)
from scripts.hooks.diff_checks_cache import (
    CACHE_FILENAME,
    CacheContext,
    lookup_run,
    python_tree_digest,
    record_run,
)


def _git(repo: Path, *args: str) -> str:
    code, output, _ = run_child(
        ("git", *args), repo, environment={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    )
    assert code == 0, output
    return output.strip()


def _write_junit(argv: Sequence[str], *, passed: int) -> None:
    named = next(part for part in argv if part.startswith("--junitxml="))
    cases = "".join(f'<testcase classname="t" name="p{i}" />' for i in range(passed))
    Path(named.split("=", 1)[1]).write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{passed}">{cases}</testsuite></testsuites>'
    )


def test_commit_amend_reuses_every_cached_subcheck_and_selector(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("value = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_mod.py").write_text("def test_ok() -> None:\n    assert True\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    cache = CacheContext(
        cache_path=tmp_path / CACHE_FILENAME,
        head_tree="tree-aaa",
        base_sha="base-111",
        python_digest="py-digest-1",
    )
    calls: list[str] = []
    selector_calls = 0

    def choose(_change: ChangeSet) -> Selection:
        nonlocal selector_calls
        selector_calls += 1
        return Selection("SELECTED", ("tests/test_mod.py",), ())

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        joined = " ".join(argv)
        calls.append(joined)
        if "meizel" in joined:
            return 0, "meizel: ok (12 fact edges, 3 manifests)\n", 5.0
        if "pytest" in joined:
            _write_junit(argv, passed=4)
            return 0, "", 2.0
        return 0, "0 errors, 0 warnings\n", 3.0

    change = ChangeSet(root=tmp_path, base="origin/main", files=("mod.py",))
    first, failed_first = check(
        change, skip_tests=False, execute=execute, choose=choose, cache=cache
    )
    assert failed_first is False
    assert selector_calls == 1
    assert len(calls) == 4

    calls.clear()
    second, failed_second = check(
        change, skip_tests=False, execute=execute, choose=choose, cache=cache
    )
    assert failed_second is False
    assert selector_calls == 1
    assert calls == []
    assert second["meizel"] == "ok(exit=0,3manifests,12edges)(0s)"
    assert second["pyright"] == "0err(0s)"
    assert second["passed"] == "4tests(0s)"
    assert first["selector"] == second["selector"] == "SELECTED:1files"


def test_doc_amend_reuses_python_checks_and_reruns_tree_checks(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("value = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Guide v1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    first_cache = CacheContext(
        cache_path=tmp_path / CACHE_FILENAME,
        head_tree="tree-v1",
        base_sha="base-111",
        python_digest="py-same",
    )
    ran_labels: list[str] = []

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        joined = " ".join(argv)
        if "basedpyright" in joined:
            ran_labels.append("pyright")
            return 0, "0 errors, 0 warnings\n", 2.0
        if "ruff" in joined:
            ran_labels.append("ruff")
            return 0, "", 1.0
        if "check_doc_references" in joined:
            ran_labels.append("docs-refs")
            return 0, "1 docs checked, 0 dangling references\n", 1.0
        if "lint_meiva_okf" in joined:
            ran_labels.append("docs-okf")
            return 0, "1 files checked, 0 error(s), 0 warning(s)\n", 1.0
        ran_labels.append("meizel")
        return 0, "meizel: ok (5 fact edges, 1 manifests)\n", 2.0

    change = ChangeSet(root=tmp_path, base="origin/main", files=("mod.py", "docs/guide.md"))
    check(
        change,
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=first_cache,
    )
    assert set(ran_labels) == {"meizel", "pyright", "ruff", "docs-refs", "docs-okf"}

    ran_labels.clear()
    (tmp_path / "docs/guide.md").write_text("# Guide v2\n")
    second_cache = CacheContext(
        cache_path=tmp_path / CACHE_FILENAME,
        head_tree="tree-v2",
        base_sha="base-111",
        python_digest="py-same",
    )
    check(
        change,
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=second_cache,
    )
    assert set(ran_labels) == {"meizel", "docs-refs", "docs-okf"}


def test_failing_or_unreadable_runs_are_never_cached(tmp_path: Path) -> None:
    (tmp_path / "meizel.toml").write_text("exports = []\n")
    cache = CacheContext(
        cache_path=tmp_path / CACHE_FILENAME,
        head_tree="tree-fail",
        base_sha="base-111",
        python_digest="py-1",
    )
    attempts = 0

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        nonlocal attempts
        attempts += 1
        if "debt" in " ".join(argv):
            return 0, "not a table\n", 1.0
        return 1, "failed\n", 1.0

    change = ChangeSet(root=tmp_path, base="origin/main", files=("meizel.toml",))
    check(
        change,
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=cache,
    )
    first_attempts = attempts
    check(
        change,
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=cache,
    )
    assert attempts == first_attempts * 2


def test_git_cache_context_requires_clean_worktree_and_survives_message_amend(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "initial")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base_sha)

    (repo / "app.py").write_text("x = 2\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "feature v1")

    before_amend = open_cache_context(repo, "origin/main")
    assert before_amend is not None

    _git(repo, "commit", "-q", "--amend", "-m", "feature v1 with receipt note")
    after_amend = open_cache_context(repo, "origin/main")
    assert after_amend is not None
    assert after_amend.head_tree == before_amend.head_tree
    assert after_amend.python_digest == before_amend.python_digest
    assert after_amend.base_sha == before_amend.base_sha

    (repo / "untracked.py").write_text("y = 1\n")
    assert open_cache_context(repo, "origin/main") is None
    (repo / "untracked.py").unlink()
    (repo / "app.py").write_text("x = 3\n")
    assert open_cache_context(repo, "origin/main") is None


def test_cache_evicts_oldest_entries_past_cap_and_cli_exposes_flags(tmp_path: Path) -> None:
    cache = CacheContext(
        cache_path=tmp_path / CACHE_FILENAME,
        head_tree="t",
        base_sha="b",
        python_digest=python_tree_digest("100644 blob abc\tmod.py\0100644 blob def\tdoc.md\0"),
    )
    for index in range(5):
        record_run(cache, f"key-{index}", code=0, output=f"out-{index}", max_entries=3)
    assert lookup_run(cache, "key-0") is None
    assert lookup_run(cache, "key-1") is None
    assert lookup_run(cache, "key-4") is not None
    raw = json.loads(cache.cache_path.read_text(encoding="utf-8"))
    assert list(raw["entries"].keys()) == ["key-2", "key-3", "key-4"]

    parsed = parse_args(["--mode", "full", "--no-cache"])
    assert parsed.mode == "full"
    assert parsed.no_cache is True


def test_incremental_mode_scopes_meizel_and_handles_deleted_or_global_inputs(
    tmp_path: Path,
) -> None:
    (tmp_path / "live.py").write_text("x = 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    invocations: list[tuple[str, ...]] = []

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        invocations.append(tuple(argv))
        return 0, "0 errors, 0 warnings", 1.0

    check(
        ChangeSet(root=tmp_path, base="origin/main", files=("live.py",)),
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
    )
    assert any(cmd[1:5] == ("-m", "meizel", "check", "--changed") for cmd in invocations)
    assert any("basedpyright" in cmd[0] and cmd[-1] == "live.py" for cmd in invocations)

    invocations.clear()
    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("deleted_only.py",)),
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
    )
    assert failed is False
    assert values["pyright"] == "0err(0s)"
    assert values["ruff"] == "ok(0s)"
    assert any(
        cmd[1:4] == ("-m", "meizel", "check") and "--changed" not in cmd for cmd in invocations
    )
    assert not any("ruff" in cmd[0] for cmd in invocations)

    invocations.clear()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    check(
        ChangeSet(root=tmp_path, base="origin/main", files=("live.py", "pyproject.toml")),
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
    )
    assert any("basedpyright" in cmd[0] and len(cmd) == 3 for cmd in invocations)


def test_full_mode_sweeps_every_layer_even_for_a_doc_only_change(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/note.md").write_text("# Note\n")
    lines = plan(
        ChangeSet(root=tmp_path, base="origin/main", files=("docs/note.md",)),
        skip_tests=True,
        mode="full",
    )
    joined = "\n".join(lines)
    for expected in (
        "meizel",
        "pyright",
        "ruff",
        "debt",
        "docs-refs",
        "docs-okf",
        "vitest-app",
        "next-build",
    ):
        assert expected in joined

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        joined_cmd = " ".join(argv)
        if "debt" in joined_cmd:
            return (
                0,
                "code  origin/main  tree  change\nambient-read  10  10  +0\ntotal  20  20  +0\n",
                1.0,
            )
        if "check_doc_references" in joined_cmd:
            return 0, "1 docs checked, 0 dangling references\n", 1.0
        if "lint_meiva_okf" in joined_cmd:
            return 0, "1 files checked, 0 error(s), 0 warning(s)\n", 1.0
        if "vitest" in joined_cmd:
            return 0, "  Tests  5 passed (5)\n", 1.0
        return 0, "meizel: ok (10 fact edges, 2 manifests)\n0 errors, 0 warnings\n", 1.0

    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("docs/note.md",)),
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        mode="full",
    )
    assert failed is False
    assert values["pyright"] == "0err(1s)"
    assert values["closure"] == "n/a(the-tree-was-green)"
    assert values["ruff"] == "ok(1s)"
    assert values["debt"] == "ok(ambient-read10->10,total20->20)(1s)"
