#!/usr/bin/env python3
# meiva:note
# Isolate a post-merge Release Train regression in `O(log K)` steps between the last certified
# `channel/latest` release and a failing train commit, executing `git bisect` inside a temporary
# detached worktree so the caller's checkout is never mutated.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# meiva:link scripts/docs/scripts.meiva.okf.md
from cli.fleet_update.plan.release_target import Channel, parse_release_tag  # noqa: E402
from scripts.host_ops.release.git_tags import (  # noqa: E402
    git_run,
    is_ancestor,
    read_certified_commit,
    read_verified_certificate,
    read_verified_channel,
    resolved_commit,
)

_FIRST_BAD_RE = re.compile(r"^([0-9a-f]{40}) is the first bad commit$", re.MULTILINE)


@dataclass(frozen=True)
class BisectPlan:
    good_ref: str
    good_sha: str
    bad_ref: str
    bad_sha: str
    commit_span: int
    max_steps: int
    pytest_target: str


def map_pytest_exit_code(returncode: int) -> int:
    if returncode == 0:
        return 0
    if returncode == 1:
        return 1
    return 125


def _resolve_good(
    repository: Path, good_ref: str, *, allow_uncertified: bool
) -> tuple[str | None, str]:
    stripped = good_ref.strip()
    if stripped.startswith("channel/"):
        ch_name = stripped.removeprefix("channel/")
        if ch_name == "stable":
            channel: Channel = "stable"
        elif ch_name == "latest":
            channel = "latest"
        else:
            return None, f"unknown channel {stripped!r}"
        _ptr, cert, reason = read_verified_channel(repository, channel)
        if cert is not None:
            return cert.sha, "verified"
        if not allow_uncertified:
            return None, f"{stripped} is not a verified channel pointer ({reason})"
    if parse_release_tag(stripped) is not None:
        cert, reason = read_verified_certificate(repository, stripped)
        if cert is not None:
            return cert.sha, "verified"
        if not allow_uncertified:
            return None, f"release tag {stripped} is uncertified ({reason})"
    sha = resolved_commit(repository, stripped)
    if sha is None:
        return None, f"good ref {stripped!r} does not resolve to a commit in {repository}"
    if allow_uncertified:
        return sha, "uncertified-allowed"
    cert, reason = read_certified_commit(repository, sha)
    if cert is None:
        return (
            None,
            f"good commit {sha[:12]} ({stripped}) is not certified ({reason}); "
            "pass --allow-uncertified-good to bisect from an uncertified commit",
        )
    return cert.sha, "verified"


def build_bisect_plan(
    repository: Path,
    *,
    good_ref: str,
    bad_ref: str,
    pytest_target: str,
    allow_uncertified_good: bool = False,
) -> tuple[BisectPlan | None, str]:
    target = pytest_target.strip()
    if not target:
        return None, "--pytest-target cannot be empty"
    good_sha, good_reason = _resolve_good(
        repository, good_ref, allow_uncertified=allow_uncertified_good
    )
    if good_sha is None:
        return None, good_reason
    bad_sha = resolved_commit(repository, bad_ref.strip())
    if bad_sha is None:
        return None, f"bad ref {bad_ref!r} does not resolve to a commit in {repository}"
    if good_sha == bad_sha:
        return None, f"good and bad resolve to the same commit ({good_sha[:12]})"
    if not is_ancestor(repository, good_sha, bad_sha):
        return (
            None,
            f"good commit {good_sha[:12]} is not an ancestor of bad commit {bad_sha[:12]}",
        )
    count_done = git_run(repository, "rev-list", "--count", f"{good_sha}..{bad_sha}")
    if count_done.returncode != 0:
        return None, f"cannot count commits in {good_sha[:12]}..{bad_sha[:12]}"
    span = int(count_done.stdout.strip())
    max_steps = max(1, span.bit_length())
    return (
        BisectPlan(
            good_ref=good_ref.strip(),
            good_sha=good_sha,
            bad_ref=bad_ref.strip(),
            bad_sha=bad_sha,
            commit_span=span,
            max_steps=max_steps,
            pytest_target=target,
        ),
        "ok",
    )


def _run_probe(worktree: Path, pytest_python: Path, pytest_target: str) -> int:
    worktree_cwd = worktree.resolve()
    cfg = worktree_cwd / "pyproject.toml"
    cfg_args = ("-c", str(cfg)) if cfg.is_file() else ()
    with tempfile.TemporaryDirectory(prefix="meiva-bisect-home-") as home:
        done = subprocess.run(  # noqa: S603
            (
                "/usr/bin/env",
                "-u",
                "PYTEST_CURRENT_TEST",
                "-u",
                "PYTEST_XDIST_WORKER",
                f"PYTHONPATH={worktree_cwd}",
                f"MEIVA_HOME={home}",
                "MEIVA_CONFIG_FETCH=skip",
                str(pytest_python),
                "-m",
                "pytest",
                *cfg_args,
                "--rootdir",
                str(worktree_cwd),
                "-q",
                "--no-header",
                pytest_target,
            ),
            cwd=worktree_cwd,
            check=False,
        )
    return map_pytest_exit_code(done.returncode)


def _default_pytest_python(repository: Path) -> Path:
    candidate = repository.resolve() / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def _execute_bisect(
    repository: Path, plan: BisectPlan, pytest_python: Path
) -> tuple[dict[str, object], int]:
    script_path = Path(__file__).resolve()
    # meiva:note
    # Preserve the leaf `python` symlink so virtualenv `pyvenv.cfg` resolution stays intact;
    # `.resolve()` on `.venv/bin/python` strips the venv and invokes the bare base interpreter.
    resolved_python = pytest_python.parent.resolve() / pytest_python.name
    with tempfile.TemporaryDirectory(prefix="meiva-bisect-wt-") as tmp_root:
        worktree = Path(tmp_root) / "wt"
        added = git_run(repository, "worktree", "add", "--detach", str(worktree), plan.bad_sha)
        if added.returncode != 0:
            detail = added.stderr.strip() or added.stdout.strip()
            return {"ok": False, "reason": "worktree-add-failed", "detail": detail}, 1
        try:
            started = git_run(worktree, "bisect", "start", plan.bad_sha, plan.good_sha)
            if started.returncode != 0:
                detail = started.stderr.strip() or started.stdout.strip()
                return {"ok": False, "reason": "bisect-start-failed", "detail": detail}, 1
            ran = git_run(
                worktree,
                "bisect",
                "run",
                sys.executable,
                str(script_path),
                "probe",
                "--worktree",
                str(worktree),
                "--pytest-python",
                str(resolved_python),
                "--pytest-target",
                plan.pytest_target,
            )
            combined = (ran.stdout + "\n" + ran.stderr).strip()
            match = _FIRST_BAD_RE.search(combined)
            if ran.returncode != 0 or match is None:
                tail = combined.splitlines()[-15:]
                return (
                    {
                        "ok": False,
                        "reason": "bisect-inconclusive",
                        "bisect_returncode": ran.returncode,
                        "good_sha": plan.good_sha,
                        "bad_sha": plan.bad_sha,
                        "output_tail": tail,
                    },
                    1,
                )
            culprit_sha = match[1]
            subject = git_run(worktree, "show", "-s", "--format=%s", culprit_sha).stdout.strip()
            return (
                {
                    "ok": True,
                    "good_sha": plan.good_sha,
                    "bad_sha": plan.bad_sha,
                    "commit_span": plan.commit_span,
                    "max_steps": plan.max_steps,
                    "pytest_target": plan.pytest_target,
                    "culprit_sha": culprit_sha,
                    "culprit_subject": subject,
                },
                0,
            )
        finally:
            git_run(worktree, "bisect", "reset")
            git_run(repository, "worktree", "remove", "--force", str(worktree))
            git_run(repository, "worktree", "prune")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or execute O(log K) git bisect over a Release Train commit range."
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    for name in ("plan", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", default=".", type=Path)
        cmd.add_argument("--good", default="channel/latest")
        cmd.add_argument("--bad", default="HEAD")
        cmd.add_argument("--pytest-target", required=True)
        cmd.add_argument("--allow-uncertified-good", action="store_true")
        if name == "run":
            cmd.add_argument("--pytest-python", default=None, type=Path)

    probe = sub.add_parser("probe")
    probe.add_argument("--worktree", required=True, type=Path)
    probe.add_argument("--pytest-python", required=True, type=Path)
    probe.add_argument("--pytest-target", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.subcommand == "probe":
        return _run_probe(args.worktree, args.pytest_python, args.pytest_target)

    plan, reason = build_bisect_plan(
        args.repo,
        good_ref=args.good,
        bad_ref=args.bad,
        pytest_target=args.pytest_target,
        allow_uncertified_good=bool(args.allow_uncertified_good),
    )
    if plan is None:
        print(json.dumps({"ok": False, "error": reason}))
        return 2
    if args.subcommand == "plan":
        print(
            json.dumps(
                {
                    "ok": True,
                    "good_ref": plan.good_ref,
                    "good_sha": plan.good_sha,
                    "bad_ref": plan.bad_ref,
                    "bad_sha": plan.bad_sha,
                    "commit_span": plan.commit_span,
                    "max_steps": plan.max_steps,
                    "pytest_target": plan.pytest_target,
                }
            )
        )
        return 0

    pytest_python = (
        args.pytest_python if args.pytest_python is not None else _default_pytest_python(args.repo)
    )
    payload, rc = _execute_bisect(args.repo, plan, pytest_python)
    print(json.dumps(payload))
    return rc


if __name__ == "__main__":
    sys.exit(main())
