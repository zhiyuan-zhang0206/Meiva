# meiva:note
# What the sub-check verdict cache promises: clean checkouts reuse passing runs across commit-message
# amends and non-Python doc edits, dirty checkouts and failing or unreadable verdicts never populate
# or read the cache, and the entry cap bounds the cache file.

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
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
    cache_key,
    full_meizel_argv,
    input_digest_for_run,
    lookup_run,
    meizel_run_key,
    python_tree_digest,
    record_run,
)
from scripts.hooks.diff_checks_plan import schedule_groups
from scripts.hooks.meizel_run import cached_full_meizel, git_executable_digest, safe_environment

# meiva:note
# The interpreter the runner hands its children is the caller's value, so these tests name it here:
# a cached verdict keyed under another interpreter's command line is a different measurement.
_PYTHON = sys.executable


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
        change,
        skip_tests=False,
        execute=execute,
        choose=choose,
        cache=cache,
        python=_PYTHON,
    )
    assert failed_first is False
    assert selector_calls == 1
    assert len(calls) == 4

    calls.clear()
    second, failed_second = check(
        change,
        skip_tests=False,
        execute=execute,
        choose=choose,
        cache=cache,
        python=_PYTHON,
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
        ran_labels.append("meizel")
        return 0, "meizel: ok (5 fact edges, 1 manifests)\n", 2.0

    change = ChangeSet(root=tmp_path, base="origin/main", files=("mod.py", "docs/guide.md"))
    check(
        change,
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=first_cache,
        python=_PYTHON,
    )
    assert set(ran_labels) == {"meizel", "pyright", "ruff", "docs-refs"}

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
        python=_PYTHON,
    )
    assert set(ran_labels) == {"meizel", "docs-refs"}


def test_failing_or_unreadable_runs_are_never_cached(tmp_path: Path) -> None:
    (tmp_path / "meizel.toml").write_text("exports = []\n")
    cache = CacheContext(
        cache_path=tmp_path / CACHE_FILENAME,
        head_tree="tree-fail",
        base_sha="base-111",
        python_digest="py-1",
        merge_base_sha="merge-1",
        meizel_input=lambda: "test-toolchain",
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
        python=_PYTHON,
    )
    first_attempts = attempts
    check(
        change,
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=cache,
        python=_PYTHON,
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
    assert after_amend.merge_base_sha == before_amend.merge_base_sha == base_sha

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
    raw = object_of(json.loads, cache.cache_path.read_text(encoding="utf-8"))
    assert is_object_dict(raw)
    entries = raw["entries"]
    assert is_object_dict(entries)
    assert list(entries) == ["key-2", "key-3", "key-4"]

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
        python=_PYTHON,
    )
    assert any(cmd[1:5] == ("-m", "meizel", "check", "--changed") for cmd in invocations)
    assert any("basedpyright" in cmd[0] and cmd[-1] == "live.py" for cmd in invocations)

    invocations.clear()
    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("deleted_only.py",)),
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        python=_PYTHON,
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
        python=_PYTHON,
    )
    assert any("basedpyright" in cmd[0] and len(cmd) == 3 for cmd in invocations)


def test_full_mode_sweeps_every_layer_even_for_a_doc_only_change(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/note.md").write_text("# Note\n")
    lines = plan(
        ChangeSet(root=tmp_path, base="origin/main", files=("docs/note.md",)),
        skip_tests=True,
        mode="full",
        python=_PYTHON,
    )
    joined = "\n".join(lines)
    for expected in (
        "meizel",
        "pyright",
        "ruff",
        "debt",
        "docs-refs",
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
        if "vitest" in joined_cmd:
            return 0, "  Tests  5 passed (5)\n", 1.0
        return 0, "meizel: ok (10 fact edges, 2 manifests)\n0 errors, 0 warnings\n", 1.0

    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("docs/note.md",)),
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        mode="full",
        python=_PYTHON,
    )
    assert failed is False
    assert values["pyright"] == "0err(1s)"
    assert values["closure"] == "n/a(the-tree-was-green)"
    assert values["ruff"] == "ok(1s)"
    assert values["debt"] == "ok(ambient-read10->10,total20->20)(1s)"


def _assert_cache_outputs_bounded(cache_path: Path) -> None:
    raw_cache = object_of(json.loads, cache_path.read_text(encoding="utf-8"))
    assert is_object_dict(raw_cache)
    raw_entries = raw_cache["entries"]
    assert is_object_dict(raw_entries)
    for entry in raw_entries.values():
        assert is_object_dict(entry)
        if entry.get("kind") == "run":
            raw_out = entry.get("output")
            assert isinstance(raw_out, str) and len(raw_out) <= 32_768


def _noisy_runner_response(argv: Sequence[str], trailing_noise: str) -> tuple[int, str, float]:
    joined = " ".join(argv)
    responses = (
        (
            "vitest",
            " Test Files  34 passed (34)\n      Tests  373 passed (373)\n" + trailing_noise,
            9.0,
        ),
        (
            "meizel debt",
            "code  origin/main  tree  change\nambient-read  10  10  +0\ntotal  20  20  +0\n"
            + trailing_noise,
            4.0,
        ),
        ("meizel check", "meizel: ok (12 fact edges, 3 manifests)\n" + trailing_noise, 5.0),
        ("check_doc_references", "2 docs checked, 0 dangling references\n" + trailing_noise, 1.0),
    )
    for marker, text, seconds in responses:
        if marker in joined:
            return 0, text, seconds
    if "pytest" in joined:
        _write_junit(argv, passed=700)
        return 0, "", 2.0
    return 0, "0 errors, 0 warnings\n", 1.0


def test_cache_replay_preserves_counts_when_output_exceeds_32kb_tail(tmp_path: Path) -> None:
    (tmp_path / "ui/web/src/lib").mkdir(parents=True)
    (tmp_path / "ui/web/src/lib/page.ts").write_text("export const x = 1;\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/note.md").write_text("# Note\n")
    (tmp_path / "meizel.toml").write_text("exports = []\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_mod.py").write_text("def test_ok() -> None:\n    assert True\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")

    cache = CacheContext(
        cache_path=tmp_path / CACHE_FILENAME,
        head_tree="tree-big-out",
        base_sha="base-111",
        python_digest="py-1",
        merge_base_sha="merge-1",
        meizel_input=lambda: "test-toolchain",
    )
    trailing_noise = "stderr/stdout trailing line beyond 32KB\n" * 1_200
    calls: list[str] = []

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        calls.append(" ".join(argv))
        return _noisy_runner_response(argv, trailing_noise)

    change = ChangeSet(
        root=tmp_path,
        base="origin/main",
        files=("ui/web/src/lib/page.ts", "docs/note.md", "meizel.toml"),
    )
    first, failed_first = check(
        change,
        skip_tests=False,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", ("tests/test_mod.py",), ()),
        cache=cache,
        python=_PYTHON,
    )
    assert failed_first is False
    assert first["vitest-app"] == "ok(373tests)(9s)"
    assert first["meizel"] == "ok(exit=0,3manifests,12edges)(5s)"
    assert first["debt"] == "ok(ambient-read10->10,total20->20)(4s)"
    assert first["docs-refs"] == "ok(2docs,0dangling)(1s)"
    assert first["passed"] == "700tests(2s)"

    _assert_cache_outputs_bounded(cache.cache_path)

    calls.clear()
    second, failed_second = check(
        change,
        skip_tests=False,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", ("tests/test_mod.py",), ()),
        cache=cache,
        python=_PYTHON,
    )
    assert failed_second is False
    assert calls == []
    assert second["vitest-app"] == "ok(373tests)(0s)"
    assert second["meizel"] == "ok(exit=0,3manifests,12edges)(0s)"
    assert second["debt"] == "ok(ambient-read10->10,total20->20)(0s)"
    assert second["docs-refs"] == "ok(2docs,0dangling)(0s)"
    assert second["passed"] == "700tests(0s)"


# meiva:note
# Unreadable vitest output must never synthesize `0tests` or enter the cache, and a legacy
# entry lacking structured `fields` must be treated as a cache miss rather than re-parsing a
# truncated tail into `0tests`.
def test_unreadable_vitest_and_legacy_entries_without_fields_never_replay_zero_tests(
    tmp_path: Path,
) -> None:
    (tmp_path / "ui/web/src/lib").mkdir(parents=True)
    (tmp_path / "ui/web/src/lib/page.ts").write_text("export const x = 1;\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")

    cache = CacheContext(
        cache_path=tmp_path / CACHE_FILENAME,
        head_tree="tree-neg",
        base_sha="base-111",
        python_digest="py-1",
        merge_base_sha="merge-1",
        meizel_input=lambda: "test-toolchain",
    )
    change = ChangeSet(root=tmp_path, base="origin/main", files=("ui/web/src/lib/page.ts",))

    vitest_calls = 0

    def unreadable_execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        nonlocal vitest_calls
        if "vitest" in " ".join(argv):
            vitest_calls += 1
            return 0, "trailing noise without summary\n", 1.0
        return 0, "meizel: ok (1 fact edges, 1 manifests)\n", 1.0

    first_unreadable, _ = check(
        change,
        skip_tests=True,
        execute=unreadable_execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=cache,
        python=_PYTHON,
    )
    second_unreadable, _ = check(
        change,
        skip_tests=True,
        execute=unreadable_execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=cache,
        python=_PYTHON,
    )
    assert first_unreadable["vitest-app"] == "unreadable(no-summary)(1s)"
    assert second_unreadable["vitest-app"] == "unreadable(no-summary)(1s)"
    assert vitest_calls == 2

    groups = schedule_groups(change, mode="incremental", cache=cache, python=_PYTHON)
    vitest_run = next(run for group in groups for run in group.runs if run.label == "vitest-app")
    vitest_key = cache_key(
        mode="incremental",
        label=vitest_run.label,
        argv=vitest_run.argv,
        cwd=vitest_run.cwd,
        input_state_digest=input_digest_for_run(
            cache, change.root, vitest_run.label, vitest_run.argv
        ),
    )
    record_run(cache, vitest_key, code=0, output="truncated tail lacking Tests summary\n")

    repaired_calls = 0

    def healthy_execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        nonlocal repaired_calls
        if "vitest" in " ".join(argv):
            repaired_calls += 1
            return (
                0,
                "      Tests  373 passed (373)\n" + ("noise\n" * 6_000),
                9.0,
            )
        return 0, "meizel: ok (1 fact edges, 1 manifests)\n", 1.0

    refreshed, failed_refreshed = check(
        change,
        skip_tests=True,
        execute=healthy_execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=cache,
        python=_PYTHON,
    )
    replayed, failed_replayed = check(
        change,
        skip_tests=True,
        execute=healthy_execute,
        choose=lambda _c: Selection("SELECTED", (), ()),
        cache=cache,
        python=_PYTHON,
    )
    assert failed_refreshed is False and failed_replayed is False
    assert repaired_calls == 1
    assert refreshed["vitest-app"] == "ok(373tests)(9s)"
    assert replayed["vitest-app"] == "ok(373tests)(0s)"


def test_debt_cache_keys_the_base_commit_merge_base_and_toolchain(tmp_path: Path) -> None:
    cache = CacheContext(
        tmp_path / CACHE_FILENAME, "tree", "base", "python", "ancestor", lambda: "toolchain"
    )
    argv = (
        _PYTHON,
        "-m",
        "meizel",
        "debt",
        "diff",
        "--against",
        cache.base_sha,
        "--on-growth",
        "warn",
    )
    original = meizel_run_key(cache, "debt", argv)
    assert original is not None
    assert meizel_run_key(replace(cache, merge_base_sha="other-ancestor"), "debt", argv) != original
    assert meizel_run_key(replace(cache, base_sha="other-base"), "debt", argv) != original
    assert (
        meizel_run_key(replace(cache, meizel_input=lambda: "other-toolchain"), "debt", argv)
        != original
    )
    assert meizel_run_key(replace(cache, merge_base_sha=""), "debt", argv) is None
    assert meizel_run_key(cache, "debt", (*argv[:-1], "fail")) != original


def test_push_reuses_full_preflight_proof_but_rejects_partial_and_zero_reports(
    tmp_path: Path,
) -> None:
    (tmp_path / "mod.py").write_text("x = 1\n")
    cache = CacheContext(
        tmp_path / CACHE_FILENAME, "tree", "base", "python", "ancestor", lambda: "toolchain"
    )
    argv = full_meizel_argv(_PYTHON)
    key = meizel_run_key(cache, "meizel", argv)
    assert key is not None
    change = ChangeSet(root=tmp_path, base="base", files=("mod.py",))

    def run(
        change: ChangeSet, execute: Callable[[Sequence[str], Path], tuple[int, str, float]]
    ) -> None:
        check(
            change,
            skip_tests=True,
            choose=lambda _change: Selection("SELECTED", (), ()),
            cache=cache,
            python=_PYTHON,
            execute=execute,
        )

    def execute(_argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        return (
            0,
            "meizel: ok (2 fact edges, 3 manifests; 1 recorded debt remains in 1 ratchet directory)\n",
            4.0,
        )

    run(change, execute)
    assert cached_full_meizel(cache, key) is None
    (tmp_path / "meizel.toml").write_text("exports = []\n")
    manifest_change = replace(change, files=("meizel.toml",))

    def empty(_argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        return 0, "meizel: ok (0 fact edges, 0 manifests)\n", 4.0

    run(manifest_change, empty)
    assert cached_full_meizel(cache, key) is None
    run(manifest_change, execute)
    assert cached_full_meizel(cache, key) == {"meizel": "ok(exit=0,3manifests,2edges)(0s)"}
    assert meizel_run_key(replace(cache, meizel_input=lambda: None), "meizel", argv) is None


def test_git_hook_environment_allows_the_default_paths_for_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "base",
        "--allow-empty",
    )
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(linked))
    environment = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "GIT_DIR": _git(linked, "rev-parse", "--absolute-git-dir"),
        "GIT_EXEC_PATH": _git(linked, "--exec-path"),
        "GIT_PREFIX": "",
    }
    assert safe_environment(linked, environment)
    assert not safe_environment(linked, {**environment, "GIT_DIR": str(repo / ".git")})
    assert not safe_environment(linked, {**environment, "GIT_INDEX_FILE": str(tmp_path / "index")})


def test_git_executable_identity_survives_hook_path_prepend(tmp_path: Path) -> None:
    user_bin = tmp_path / "bin"
    exec_bin = tmp_path / "git-core"
    user_bin.mkdir()
    exec_bin.mkdir()
    for directory in (user_bin, exec_bin):
        executable = directory / "git"
        executable.write_bytes(b"git executable version 1")
        executable.chmod(0o755)
    preflight = git_executable_digest(str(user_bin))
    hook_path = f"{exec_bin}:{user_bin}"
    assert git_executable_digest(hook_path) == preflight
    (exec_bin / "git").write_bytes(b"git executable version 2")
    assert git_executable_digest(hook_path) != preflight
