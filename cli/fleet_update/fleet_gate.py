# meiva:note
# What a window reads locally and on each host before it stops anything: the certified release
# target in the operator's repository, the host probe, and the helper facts of the commit it
# switches to. The decisions on what these reads refuse live in `plan/release_target.py`,
# `plan/host_probe.py` and `plan/helper_gate.py`. Like the rest of `cli.fleet_update`, nothing here
# imports application code, so a window still runs from a bare operator checkout.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .plan.host_commands import PROBE, helper_probe_command
from .plan.host_probe import facts_of, was_serving
from .plan.release_target import (
    ResolvedTarget,
    TargetRequest,
    TargetSnapshot,
    resolve_update_target,
)
from .session import RefusedError, Session


def git_in_repo(*args: str, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _peeled_commit(repo: Path, rev: str) -> str | None:
    done = git_in_repo("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", repo=repo)
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else None


def _tag_annotation_body(repo: Path, tag_name: str) -> str | None:
    ref = f"refs/tags/{tag_name}"
    kind = git_in_repo("cat-file", "-t", ref, repo=repo)
    if kind.returncode != 0 or kind.stdout.strip() != "tag":
        return None
    payload = git_in_repo("cat-file", "-p", ref, repo=repo)
    if payload.returncode != 0:
        return None
    _headers, sep, body = payload.stdout.partition("\n\n")
    return body if sep else None


def _tags_pointing_at(repo: Path, commit_sha: str | None) -> dict[str, str | None]:
    if commit_sha is None:
        return {}
    done = git_in_repo("tag", "--points-at", commit_sha, repo=repo)
    if done.returncode != 0:
        return {}
    return {
        line.strip(): _tag_annotation_body(repo, line.strip())
        for line in done.stdout.splitlines()
        if line.strip()
    }


def _snapshot_tag(repo: Path, tag_name: str, *, include_candidates: bool) -> TargetSnapshot:
    peeled = _peeled_commit(repo, f"refs/tags/{tag_name}")
    body = _tag_annotation_body(repo, tag_name) if peeled is not None else None
    candidates = _tags_pointing_at(repo, peeled) if include_candidates else {}
    return TargetSnapshot(
        repo_display=str(repo),
        peeled_sha=peeled,
        tag_body=body,
        candidate_tags=candidates,
    )


def _snapshot_branch(repo: Path, branch: str, commit_rev: str | None) -> TargetSnapshot:
    tip = _peeled_commit(repo, f"refs/remotes/origin/{branch}")
    pinned = _peeled_commit(repo, commit_rev) if commit_rev is not None else None
    on_branch = (
        pinned is not None
        and tip is not None
        and git_in_repo("merge-base", "--is-ancestor", pinned, tip, repo=repo).returncode == 0
    )
    return TargetSnapshot(
        repo_display=str(repo),
        branch_tip_sha=tip,
        pinned_sha=pinned,
        pinned_on_branch=on_branch,
    )


def _build_snapshot(repo: Path, request: TargetRequest) -> TargetSnapshot:
    if request.target_mode == "channel" and request.channel is not None:
        return _snapshot_tag(repo, f"channel/{request.channel}", include_candidates=True)
    if request.target_mode == "release" and request.release_tag is not None:
        return _snapshot_tag(repo, request.release_tag, include_candidates=False)
    if request.target_mode == "commit" and request.commit_rev is not None:
        peeled = _peeled_commit(repo, request.commit_rev)
        return TargetSnapshot(
            repo_display=str(repo),
            peeled_sha=peeled,
            candidate_tags=_tags_pointing_at(repo, peeled),
        )
    if request.target_mode == "track-branch" and request.branch is not None:
        return _snapshot_branch(repo, request.branch, request.commit_rev)
    return TargetSnapshot(repo_display=str(repo))


def resolve_target(repo: Path, request: TargetRequest) -> ResolvedTarget:
    resolved, reason = resolve_update_target(request, _build_snapshot(repo, request))
    if resolved is None:
        raise RefusedError(reason)
    return resolved


# meiva:note
# One host's probe facts, and whether it was serving when the window began.
def probe_host(s: Session, alias: str) -> tuple[dict[str, str], bool]:
    facts = facts_of(s.run(alias, PROBE, effect=False))
    return facts, was_serving(facts)


def probe_helper(s: Session, alias: str, new: str) -> dict[str, str]:
    return facts_of(s.run(alias, helper_probe_command(new), effect=False))


# meiva:note
# The identity this fleet records for each host, read from the tracked table at call time: the
# table is data a pull request changes, never something a window writes.
def baselines(repo: Path) -> dict[str, str]:
    table: dict[str, str] = json.loads(
        (repo / "deploy" / "fleet" / "helper-identities.json").read_text()
    )
    return table
