# meiva:note
# The round end to end, against a throwaway repository: the gate-closed night, a never-list
# entry, and a tree whose HEAD is detached. None of these reaches `gh`, so the suite answers
# fast and offline — which is the point of the paths being values.
# meiva:link scripts/host_ops/docs/worktree-gc.meiva.okf.md

from __future__ import annotations

import json
import subprocess

from scripts.host_ops.worktree_gc.cli import main


def git(cwd, *args) -> str:
    # meiva:note
    # The suite's only doorway to git: a fixed argv against the throwaway repository it just made.
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "first")
    return repo


def base_args(tmp_path, repo, *extra):
    return [
        "--repo",
        str(repo),
        "--probe",
        "",
        "--ws",
        str(tmp_path / "ws"),
        "--ledger",
        str(tmp_path / "ledger.jsonl"),
        "--never",
        str(tmp_path / "never.txt"),
        "--blind-state",
        str(tmp_path / "blind.json"),
        "--hold",
        str(tmp_path / "hold.json"),
        "--manifests",
        str(tmp_path / "manifests.json"),
        "--home-env",
        str(tmp_path / ".env"),
        "--max",
        "1",
        *extra,
    ]


def ledger_rows(tmp_path):
    text = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_gate_closed_round_records_a_comfortable_volume(tmp_path, capsys) -> None:
    repo = make_repo(tmp_path)
    assert main(base_args(tmp_path, repo, "--trigger-avail-gib", "0")) == 0
    out = capsys.readouterr().out
    assert "gate(avail<0G" in out and "-> closed" in out
    assert "judge: {" in out
    assert [row["result"] for row in ledger_rows(tmp_path)] == ["skip-disk-comfortable"]


def test_never_list_entry_skips_the_tree_without_touching_it(tmp_path, capsys) -> None:
    repo = make_repo(tmp_path)
    worktree = tmp_path / "one"
    git(repo, "worktree", "add", "-b", "feature", str(worktree))
    (tmp_path / "never.txt").write_text("one\n", encoding="utf-8")
    main(base_args(tmp_path, repo, "--trigger-avail-gib", "99999"))
    out = capsys.readouterr().out
    assert "skipped-never-list" in out
    assert worktree.is_dir()
    assert {row["result"] for row in ledger_rows(tmp_path)} >= {"skipped-never-list"}


def test_detached_head_is_never_a_candidate(tmp_path, capsys) -> None:
    # meiva:note
    # A detached HEAD has no branch whose PR could prove the work is merged, so the gate refuses
    # it before asking `gh` anything.
    repo = make_repo(tmp_path)
    worktree = tmp_path / "detached"
    git(repo, "worktree", "add", "--detach", str(worktree))
    main(base_args(tmp_path, repo, "--trigger-avail-gib", "99999"))
    out = capsys.readouterr().out
    assert "no-branch" in out
    assert worktree.is_dir()
