# meiva:note
# Which tool a push selects for the frontend, and what it hands that tool.
# The CI-base incremental selection (`--base`) exercises the selector as a process against a
# throwaway repository: the gate that keeps a whole-repository check off a push that cannot move it,
# the disk-consumer table whose entries have no import edge, and the schema/type artifacts that keep
# their own freshness contract. The wrappers around it live in `test_prepush_hook_runs.py`;
# `test_prepush_hooks.py` holds the guard, install and stage contracts.

# meiva:note
# Every subprocess call in this module runs git, bash or this interpreter against a throwaway
# repository built from fixture literals, so S603 has no untrusted command to flag.
# ruff: noqa: S603

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci.prepush.prepush_probe import hook_table

ROOT = Path(__file__).resolve().parents[3]
SELECTOR = ROOT / "scripts/provision/prepush_frontend.py"

# meiva:note
# Frontend contribution scope across real rebase and pre-commit push ranges.


_GIT_HOOK_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def _clean_git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_HOOK_VARS}


def _repo_env(repo: Path, **overrides: str) -> dict[str, str]:
    probe = repo.parent / "probe"
    locks = repo.parent / "locks"
    base = _clean_git_env()
    return {
        **base,
        "PATH": f"{probe}{os.pathsep}{base['PATH']}",
        "CALLS": str(repo / "calls.jsonl"),
        "MEIVA_PREPUSH_MAX_LOAD_PER_CORE": "1000000",
        "MEIVA_PREPUSH_LOCK_DIR": str(locks),
        **overrides,
    }


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_clean_git_env(),
    ).stdout.strip()


def commit(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fixture change")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    result = tmp_path / "repo"
    result.mkdir()
    git(result, "init", "-q", "-b", "main")
    git(result, "config", "user.email", "test@example.com")
    git(result, "config", "user.name", "Test")
    commit(result, ".gitignore", "node_modules/\n")
    git(result, "update-ref", "refs/remotes/origin/main", "HEAD")
    bins = result / "ui/web/node_modules/.bin"
    bins.mkdir(parents=True)
    for name in ("vitest", "eslint", "next", "tsc"):
        (bins / name).symlink_to(sys.executable)
    probe = tmp_path / "probe"
    probe.mkdir()
    for name in ("node", "pnpm"):
        (probe / name).write_text(
            f"#!{sys.executable}\nimport os,json,sys\n"
            "if os.path.basename(sys.argv[0]) == 'pnpm':\n"
            " with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            " raise SystemExit(int(os.environ.get('TOOL_STATUS', '0')))\n"
            "sys.stdin.read()\n"
        )
        (probe / name).chmod(0o755)
    return result


def select(repo: Path, tool: str, *, tool_status: str = "0") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SELECTOR), tool],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=_repo_env(repo, TOOL_STATUS=tool_status),
    )


def calls(repo: Path) -> list[list[str]]:
    log = repo / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("tool", ["tsc", "eslint", "vitest"])
def test_rebased_upstream_ui_does_not_invoke_tool(repo: Path, tool: str) -> None:
    git(repo, "checkout", "-qb", "contribution")
    commit(repo, "notes.md", "own docs")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    commit(repo, "ui/web/src/upstream.ts", "export const x = 1")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "contribution")
    git(repo, "rebase", "origin/main")
    assert "ui/web/src/upstream.ts" in git(repo, "diff", "--name-only", old, "HEAD")
    # meiva:note
    # What is exercised is the pre-commit old-tip selection itself, not just the selector function.
    config = repo / ".pre-commit-config.yaml"
    legacy_command = {
        "tsc": "pnpm exec next typegen && pnpm exec tsc --noEmit",
        "eslint": "pnpm run lint",
        "vitest": "pnpm exec vitest run",
    }[tool]
    config.write_text(
        "repos:\n- repo: local\n  hooks:\n  - id: frontend\n    name: frontend\n"
        f"    entry: bash {ROOT / 'scripts/hooks/prepush-guard.sh'} {tool} -- bash -c 'cd ui/web && {legacy_command}'\n"
        "    language: system\n    stages: [pre-push]\n    files: ^ui/web/.*\\.(ts|tsx)$\n    pass_filenames: false\n"
    )
    legacy = subprocess.run(
        [
            str(ROOT / ".venv/bin/pre-commit"),
            "run",
            "--hook-stage",
            "pre-push",
            "--from-ref",
            old,
            "--to-ref",
            "HEAD",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=_repo_env(repo),
    )
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr
    assert calls(repo), "the old push-tip trigger must reproduce the upstream-only tool invocation"
    (repo / "calls.jsonl").unlink()
    config.write_text(
        "repos:\n- repo: local\n  hooks:\n  - id: frontend\n    name: frontend\n"
        f"    entry: {sys.executable} {SELECTOR} {tool}\n"
        "    language: system\n    stages: [pre-push]\n    always_run: true\n    pass_filenames: false\n"
    )
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/pre-commit"),
            "run",
            "--hook-stage",
            "pre-push",
            "--from-ref",
            old,
            "--to-ref",
            "HEAD",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=_repo_env(repo),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert calls(repo) == []


def test_new_branch_runs_own_source_and_disk_consumer(repo: Path) -> None:
    # meiva:note
    # The contribution branch exists locally only: no remote tracking branch is set up for it.
    git(repo, "checkout", "-qb", "new-contribution")
    commit(repo, "ui/web/src/widget.tsx", "export const x = 1")
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert calls(repo) == [
        [
            "exec",
            "vitest",
            "run",
            "--config",
            "tests/vitest.config.mts",
            "--passWithNoTests=false",
            "src/lib/auth/localstorage-policy.test.ts",
        ],
        [
            "exec",
            "vitest",
            "related",
            "--run",
            "--config",
            "tests/vitest.config.mts",
            "--passWithNoTests=false",
            "src/widget.tsx",
        ],
    ]


@pytest.mark.parametrize(
    "generated", ["ui/web/openapi.json", "ui/web/src/lib/contracts/types-generated.ts"]
)
def test_schema_type_artifacts_keep_contract_checks_without_related_run(
    repo: Path, generated: str
) -> None:
    assert re.search(hook_table()["types-codegen-fresh"]["files"], generated)
    commit(repo, generated, "{}" if generated.endswith(".json") else "export interface Example {}")
    result = select(repo, "tsc")
    assert result.returncode == 0, result.stderr
    assert calls(repo)[-1] == ["exec", "tsc", "--noEmit"]
    (repo / "calls.jsonl").unlink()
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert all("related" not in command for command in calls(repo))
    if generated.endswith(".ts"):
        assert calls(repo)[0][-1] == "src/lib/auth/localstorage-policy.test.ts"
    else:
        assert "require tsc and codegen freshness" in result.stdout


def test_type_artifact_does_not_hide_changed_runtime_source(repo: Path) -> None:
    commit(repo, "ui/web/src/lib/contracts/types-generated.ts", "export interface Example {}")
    commit(repo, "ui/web/scripts/runtime.mjs", "export const x = 1")
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert calls(repo)[-1] == [
        "exec",
        "vitest",
        "related",
        "--run",
        "--config",
        "tests/vitest.config.mts",
        "--passWithNoTests=false",
        "scripts/runtime.mjs",
    ]


def test_deleted_and_renamed_inputs_remain_visible(repo: Path) -> None:
    commit(repo, "ui/web/src/old name.ts", "export const x = 1")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "mv", "ui/web/src/old name.ts", "ui/web/src/new name.ts")
    git(repo, "commit", "-qm", "rename")
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert "deleted-input closure requires CI" in result.stderr
    assert calls(repo)[-1][-1] == "src/new name.ts"
    (repo / "calls.jsonl").unlink()
    result = select(repo, "eslint")
    assert result.returncode == 0, result.stderr
    assert calls(repo)[0][-1] == "src/new name.ts"
    assert "src/old name.ts" not in calls(repo)[0]


def test_global_inputs_report_ci_gap_and_keep_known_disk_test(repo: Path) -> None:
    commit(repo, "ui/web/package.json", "{}")
    result = select(repo, "vitest")
    assert result.returncode == 0
    assert "UNVERIFIED" in result.stderr
    assert calls(repo) == [
        [
            "exec",
            "vitest",
            "run",
            "--config",
            "tests/vitest.config.mts",
            "--passWithNoTests=false",
            "src/lib/transport/frontend-bind.test.ts",
        ]
    ]


@pytest.mark.parametrize("tool", ["tsc", "eslint", "vitest"])
def test_missing_base_is_not_no_changes(repo: Path, tool: str) -> None:
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    result = select(repo, tool)
    assert result.returncode != 0
    assert "requires local origin/main" in result.stderr
    assert "no tool invoked" not in result.stdout
    assert calls(repo) == []


def test_empty_or_failed_related_run_stays_failed(repo: Path) -> None:
    commit(repo, "ui/web/scripts/changed.mjs", "export const x = 1")
    result = select(repo, "vitest", tool_status="1")
    assert result.returncode == 1
    assert calls(repo)[0][2:7] == [
        "related",
        "--run",
        "--config",
        "tests/vitest.config.mts",
        "--passWithNoTests=false",
    ]


@pytest.mark.parametrize(
    ("path", "consumer"),
    [
        ("services/entrypoints/gate/static/login.html", "src/lib/transport/gate-login.test.ts"),
        ("tests/fixtures/events/chat/chat_delta.json", "src/lib/contracts/event-fixtures.test.ts"),
        (
            "base/packages/plugins/surface/ui_contributions.py",
            "src/components/plugins/plugin-nav-icon.test.ts",
        ),
        ("ui/app/app-ui/locales/en.js", "src/app/app-ui-locale.test.ts"),
        ("ui/web/messages/en/core.json", "src/i18n/messages-layout.test.ts"),
        ("ui/web/src/app/globals.css", "src/app/globals-font-stack.test.ts"),
    ],
)
def test_known_filesystem_consumers_run_without_import_edge(
    repo: Path, path: str, consumer: str
) -> None:
    commit(repo, path, "fixture")
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert consumer in calls(repo)[0]
    assert calls(repo)[0][2] == "run"


def test_delete_only_input_checks_type_project_and_reports_test_closure(repo: Path) -> None:
    commit(repo, "ui/web/src/removed.ts", "export const x = 1")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "rm", "ui/web/src/removed.ts")
    git(repo, "commit", "-qm", "delete")
    result = select(repo, "tsc")
    assert result.returncode == 0, result.stderr
    assert calls(repo) == [["exec", "next", "typegen"], ["exec", "tsc", "--noEmit"]]
    (repo / "calls.jsonl").unlink()
    result = select(repo, "vitest")
    assert "deleted-input closure requires CI" in result.stderr
    assert calls(repo) == [
        [
            "exec",
            "vitest",
            "run",
            "--config",
            "tests/vitest.config.mts",
            "--passWithNoTests=false",
            "src/lib/auth/localstorage-policy.test.ts",
        ]
    ]


def test_eslint_pipeline_preserves_native_failure(repo: Path) -> None:
    commit(repo, "ui/web/src/changed.ts", "export const x = 1")
    result = select(repo, "eslint", tool_status="7")
    assert result.returncode == 7
    assert calls(repo)[0][-1] == "src/changed.ts"


def test_ci_base_mode_runs_incremental_eslint_conditional_build_and_vitest(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "ui/web/src/components/plain.ts", "export const plain = 1")
    result = subprocess.run(
        [sys.executable, str(SELECTOR), "--base", base],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=_repo_env(repo),
    )
    assert result.returncode == 0, result.stderr
    recorded = calls(repo)
    assert ["run", "build"] not in recorded
    assert any(
        cmd[:2] == ["exec", "eslint"] and "src/components/plain.ts" in cmd for cmd in recorded
    )
    assert any(
        cmd[:6]
        == [
            "exec",
            "vitest",
            "related",
            "--run",
            "--config",
            "tests/vitest.config.mts",
        ]
        and "--passWithNoTests" in cmd
        for cmd in recorded
    )

    (repo / "calls.jsonl").unlink()
    commit(
        repo,
        "ui/web/src/app/dashboard/page.tsx",
        '"use client";\nexport default function P() { return null; }',
    )
    result_app = subprocess.run(
        [sys.executable, str(SELECTOR), "--base", base],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=_repo_env(repo),
    )
    assert result_app.returncode == 0, result_app.stderr
    recorded_app = calls(repo)
    assert ["run", "build"] in recorded_app
    assert ["run", "check:bundle-integrity"] in recorded_app
