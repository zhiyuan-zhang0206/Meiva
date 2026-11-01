# meiva:note
# What the hook wrappers run when a commit or a push happens.
# The nested branch-diff run that takes no load threshold, the commit-stage ESLint run over
# changed files, and the artifact hooks whose inputs a branch only deleted. Each is exercised as a
# process against a throwaway repository; `test_prepush_selection.py` holds the CI-base selection
# those wrappers hand to `scripts/provision/prepush_frontend.py`, and `test_prepush_hooks.py` holds
# the guard, install and stage contracts.

# meiva:note
# Every subprocess call in this module runs git, bash or this interpreter against a throwaway
# repository built from fixture literals, so S603 has no untrusted command to flag.
# ruff: noqa: S603

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.ci.prepush.prepush_probe import hook_table, init_probe_repo, init_repo_with_origin_main

ROOT = Path(__file__).resolve().parents[3]
BRANCH_LINT = ROOT / "scripts/hooks/prepush-branch-lint.sh"
FRESHNESS = ROOT / "scripts/provision/prepush_freshness.py"
PRECOMMIT_ESLINT = ROOT / "scripts/hooks/precommit-eslint.sh"
MODULE = ROOT / "scripts/provision/prepush_branch_diff.py"


# meiva:note
# The nested branch-diff run.


def test_branch_lint_skips_without_precommit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo_with_origin_main(repo)
    result = subprocess.run(
        ["bash", str(BRANCH_LINT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert "PRE-PUSH SKIPPED [branch-lint]: missing .venv/bin/pre-commit" in result.stderr


def test_branch_lint_runs_whatever_the_load_and_without_a_lock(tmp_path: Path) -> None:
    # meiva:note
    # The nested run costs about one commit over the same files, so a load threshold would only
    # make rebased commits go unchecked at random: with the threshold at zero and no lock
    # directory, it still runs the commit stage over merge-base..HEAD.
    repo = tmp_path / "repo"
    repo.mkdir()
    base_sha = init_repo_with_origin_main(repo)
    (repo / "change.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "change"], check=True)
    argv_log = tmp_path / "pre-commit-argv.json"
    fake = repo / ".venv/bin/pre-commit"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys\njson.dump(sys.argv[1:], open({str(argv_log)!r}, 'w'))\n"
    )
    fake.chmod(0o755)
    (repo / ".venv/bin/python").symlink_to(sys.executable)
    (repo / "scripts/provision").mkdir(parents=True)
    shutil.copy(MODULE, repo / "scripts/provision/prepush_branch_diff.py")
    result = subprocess.run(
        ["bash", str(BRANCH_LINT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "MEIVA_PREPUSH_MAX_LOAD_PER_CORE": "0"},
    )
    assert result.returncode == 0, result.stderr
    assert "SKIPPED" not in result.stderr
    # meiva:note
    # The recorded argv is compared whole, one flag per line, so the formatter leaves it alone
    # rather than re-wrapping the literal this test reads back.
    assert json.loads(argv_log.read_text()) == [
        "run",
        "--color",
        "never",
        "--hook-stage",
        "pre-commit",
        "--from-ref",
        base_sha,
        "--to-ref",
        "HEAD",
    ]


# meiva:note
# prepush-meizel.sh runs meizel once at the pre-push stage, so prepush-branch-lint.sh appends
# meizel-check-changed to SKIP without dropping any hooks the caller already asked pre-commit to skip.
@pytest.mark.parametrize(
    ("caller_skip", "expected_skip"),
    [
        (None, "meizel-check-changed"),
        ("ruff,trailing-whitespace", "ruff,trailing-whitespace,meizel-check-changed"),
    ],
)
def test_branch_lint_skips_commit_meizel_and_preserves_caller_skip(
    tmp_path: Path, caller_skip: str | None, expected_skip: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo_with_origin_main(repo)
    (repo / "change.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "change"], check=True)
    skip_log = tmp_path / "pre-commit-skip.json"
    fake = repo / ".venv/bin/pre-commit"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        f"#!{sys.executable}\nimport json, os\njson.dump(os.environ.get('SKIP'), open({str(skip_log)!r}, 'w'))\n"
    )
    fake.chmod(0o755)
    (repo / ".venv/bin/python").symlink_to(sys.executable)
    (repo / "scripts/provision").mkdir(parents=True)
    shutil.copy(MODULE, repo / "scripts/provision/prepush_branch_diff.py")
    env = {name: value for name, value in os.environ.items() if name != "SKIP"}
    if caller_skip is not None:
        env["SKIP"] = caller_skip
    result = subprocess.run(
        ["bash", str(BRANCH_LINT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(skip_log.read_text()) == expected_skip


def test_eslint_keeps_commit_hook_and_scopes_push_contribution() -> None:
    # meiva:note
    # Keep the existing commit check and CI-compatible push hook ID.
    hooks = hook_table()
    changed, whole = hooks["frontend-eslint"], hooks["frontend-eslint-full"]
    assert "stages" not in changed, "the changed-files run belongs to the commit stage"
    assert changed["entry"] == "bash scripts/hooks/precommit-eslint.sh"
    assert "pass_filenames" not in changed, "it must be handed the changed files"
    assert whole["stages"] == ["pre-push"]
    assert whole["pass_filenames"] is False
    assert whole["entry"] == ".venv/bin/python scripts/provision/prepush_frontend.py eslint"


# meiva:note
# The commit-stage ESLint run over changed files.


@pytest.fixture
def eslint_repo(tmp_path: Path) -> tuple[Path, Path]:
    # meiva:note
    # A repo with fake `pnpm` / `node` that log how they were called, so the script's own
    # decisions (which files, whole project or not) can be read back. Returns (repo, call log dir).
    repo = tmp_path / "repo"
    (repo / "ui/web/node_modules/.bin").mkdir(parents=True)
    (repo / "ui/web/node_modules/.bin/eslint").write_text("")
    (repo / "ui/web/node_modules/.bin/eslint").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    calls = tmp_path / "calls"
    calls.mkdir()
    shims = tmp_path / "shims"
    shims.mkdir()
    for tool in ("pnpm", "node"):
        shim = shims / tool
        shim.write_text(f'#!/bin/sh\necho "{tool} $*" >> {calls / tool}\ncat > /dev/null\n')
        shim.chmod(0o755)
    return repo, calls


def _tool_calls(calls: Path, tool: str) -> list[str]:
    log = calls / tool
    return log.read_text().splitlines() if log.exists() else []


def _run_eslint_script(repo: Path, calls: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{calls.parent / 'shims'}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(PRECOMMIT_ESLINT), *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env=env,
    )


def test_eslint_lints_just_the_changed_files(eslint_repo: tuple[Path, Path]) -> None:
    repo, calls = eslint_repo
    result = _run_eslint_script(repo, calls, "ui/web/src/a.tsx", "ui/web/src/lib/b.ts")
    assert result.returncode == 0, result.stderr
    assert _tool_calls(calls, "pnpm") == [
        "pnpm exec eslint --no-warn-ignored --max-warnings 0 src/a.tsx src/lib/b.ts"
    ]
    assert _tool_calls(calls, "node") == []


@pytest.mark.parametrize(
    "setup_path",
    [
        "ui/web/eslint.config.mjs",
        "ui/web/eslint-rules/sentence-case.mjs",
        "ui/web/package.json",
        "ui/web/pnpm-lock.yaml",
        "ui/web/pnpm-workspace.yaml",
        "ui/web/tsconfig.json",
    ],
)
def test_eslint_setup_change_lints_the_whole_project(
    eslint_repo: tuple[Path, Path], setup_path: str
) -> None:
    # meiva:note
    # The config, local rules and dependencies decide the verdict of files
    # that did not change, so editing one of them (even next to a source file) is a full run.
    repo, calls = eslint_repo
    result = _run_eslint_script(repo, calls, "ui/web/src/a.tsx", setup_path)
    assert result.returncode == 0, result.stderr
    assert _tool_calls(calls, "pnpm") == ["pnpm run lint"]
    assert _tool_calls(calls, "node") == []


def test_eslint_hook_triggers_on_every_setup_file() -> None:
    # meiva:note
    # Every path the script widens on must reach it: the hook's `files:` selects them.
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = {h["id"]: h for r in config["repos"] for h in r["hooks"]}["frontend-eslint"]
    selected = re.compile(hook["files"])
    for path in re.findall(r"ui/web/[\w./*-]+", PRECOMMIT_ESLINT.read_text().split("case", 1)[1]):
        assert selected.search(path.replace("*", "x.mjs")), path


def test_eslint_skips_loudly_without_node_modules(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = subprocess.run(
        ["bash", str(PRECOMMIT_ESLINT), "ui/web/src/a.tsx"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0
    assert "ESLINT SKIPPED: missing ui/web/node_modules" in result.stderr


# meiva:note
# The artifact hooks whose inputs a branch only deleted.


@pytest.fixture
def freshness_repo(tmp_path: Path) -> tuple[Path, Path]:
    # meiva:note
    # A repo carrying the real hook config and a copy of the freshness script, with a fake
    # `.venv/bin/pre-commit` that logs the hook it was asked to run. Returns (repo, call log).
    repo = tmp_path / "repo"
    (repo / "scripts/provision").mkdir(parents=True)
    (repo / "scripts/hooks").mkdir(parents=True)
    (repo / "scripts/provision/prepush_freshness.py").write_text(FRESHNESS.read_text())
    (repo / "scripts/hooks/prepush-base.sh").write_text(
        (ROOT / "scripts/hooks/prepush-base.sh").read_text()
    )
    (repo / ".pre-commit-config.yaml").write_text((ROOT / ".pre-commit-config.yaml").read_text())
    calls = tmp_path / "calls.log"
    fake = repo / ".venv/bin/pre-commit"
    fake.parent.mkdir(parents=True)
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)
    (repo / "ui/web/node_modules").mkdir(parents=True)
    return repo, calls


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)


def _run_freshness(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/provision/prepush_freshness.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _hooks_run(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [line.split()[-1] for line in calls.read_text().splitlines()]


def _branch_from(repo: Path, files: dict[str, str]) -> None:
    # meiva:note
    # Commit `files` as the shared base, mark it origin/main, and leave HEAD on it.
    init_probe_repo(repo)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    (repo / ".gitignore").write_text(".venv/\nui/web/node_modules/\n")
    _commit_all(repo, "base")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha], check=True
    )


def test_a_branch_that_only_deletes_an_input_reruns_that_hook_over_the_repo(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    _branch_from(repo, {"docs/gone.meiva.okf.md": "x\n", "README.txt": "x\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "docs/gone.meiva.okf.md"], check=True)
    _commit_all(repo, "delete only")
    result = _run_freshness(repo)
    assert result.returncode == 0, result.stderr
    # meiva:note
    # The OKF doc is gone and nothing added or changed a markdown path: lint-meiva-okf and
    # check-doc-references both match the deleted path, and no other path of the branch.
    assert sorted(_hooks_run(calls)) == ["check-doc-references", "lint-meiva-okf"]
    assert "--all-files" in calls.read_text()


def test_a_whole_repo_hook_is_left_to_the_branch_diff_run_when_the_branch_also_changes_an_input(
    freshness_repo: tuple[Path, Path],
) -> None:
    # meiva:note
    # The nested branch-diff run executes a whole-repository hook once the branch added or
    # changed one of its inputs, and that run judges the deletion with it: no repeat.
    repo, calls = freshness_repo
    _branch_from(repo, {"base/config/gone.py": "x = 1\n", "base/config/kept.py": "x = 1\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "base/config/gone.py"], check=True)
    (repo / "base/config/kept.py").write_text("x = 2\n")
    _commit_all(repo, "delete one config module, change another")
    assert _run_freshness(repo).returncode == 0
    assert _hooks_run(calls) == []


def test_a_per_file_hook_sees_the_deletion_only_through_the_sweep(
    freshness_repo: tuple[Path, Path],
) -> None:
    # meiva:note
    # lint-meiva-okf judges only the documents it is handed, so a branch that deletes one OKF
    # document and changes another still needs the whole-set re-check for links into the deleted
    # one; the whole-repository markdown check is already covered by the nested run.
    repo, calls = freshness_repo
    _branch_from(repo, {"docs/gone.meiva.okf.md": "x\n", "docs/kept.meiva.okf.md": "x\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "docs/gone.meiva.okf.md"], check=True)
    (repo / "docs/kept.meiva.okf.md").write_text("changed\n")
    _commit_all(repo, "delete one, change another")
    assert _run_freshness(repo).returncode == 0
    assert _hooks_run(calls) == ["lint-meiva-okf"]


def test_moving_an_input_out_of_a_hook_pattern_counts_as_deleting_it(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    _branch_from(repo, {"docs/a.meiva.okf.md": "same text\n" * 5})
    subprocess.run(["git", "-C", str(repo), "mv", "docs/a.meiva.okf.md", "docs/a.md"], check=True)
    _commit_all(repo, "rename out of the OKF pattern")
    assert _run_freshness(repo).returncode == 0
    # meiva:note
    # The old path leaves the OKF pattern; the new one is still markdown, which the branch-diff
    # run hands to check-doc-references, so only the OKF hook is left for the sweep.
    assert _hooks_run(calls) == ["lint-meiva-okf"]


def test_a_branch_touching_no_artifact_input_runs_nothing(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    _branch_from(repo, {"notes/a.txt": "x\n"})
    (repo / "notes/a.txt").write_text("changed\n")
    _commit_all(repo, "unrelated")
    result = _run_freshness(repo)
    assert result.returncode == 0, result.stderr
    assert _hooks_run(calls) == []
    assert "nothing to add" in result.stdout


def test_an_unknown_range_rejects_artifact_selection(freshness_repo: tuple[Path, Path]) -> None:
    repo, calls = freshness_repo
    init_probe_repo(repo)
    _commit_all(repo, "base")
    result = _run_freshness(repo)
    assert result.returncode != 0
    assert _hooks_run(calls) == []
    assert "requires local origin/main" in result.stderr


def test_types_codegen_is_skipped_loudly_without_node_modules(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    subprocess.run(["rm", "-rf", str(repo / "ui/web/node_modules")], check=True)
    init_probe_repo(repo)
    _branch_from(repo, {"gateway/removed.py": "x = 1\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "gateway/removed.py"], check=True)
    _commit_all(repo, "delete codegen input")
    result = _run_freshness(repo)
    assert "PRE-PUSH SKIPPED [types-codegen-fresh]: missing ui/web/node_modules" in result.stderr
    assert "types-codegen-fresh" not in _hooks_run(calls)


def test_a_failing_artifact_hook_fails_the_push(freshness_repo: tuple[Path, Path]) -> None:
    repo, calls = freshness_repo
    (repo / ".venv/bin/pre-commit").write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit 1\n')
    _branch_from(repo, {"gateway/removed.py": "x = 1\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "gateway/removed.py"], check=True)
    _commit_all(repo, "delete codegen input")
    assert _run_freshness(repo).returncode == 1
