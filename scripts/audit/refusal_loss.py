#!/usr/bin/env python3
# meiva:link scripts/audit/docs/refusal-loss.meiva.okf.md

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from base.host.proc import run_bounded
from scripts.audit.analysis.refusal_loss import (
    DELETION,
    UNREPLACED,
    Finding,
    Hunk,
    blocking,
    findings_for,
    manifest_delta,
    parse_diff,
    refusal_messages,
    summary_line,
)

GIT_TIMEOUT_S = 120.0
MANIFESTS = "*meizel.toml"


def _git(repo: Path, *args: str) -> str:
    done = run_bounded(
        ["git", *args], timeout=GIT_TIMEOUT_S, capture_output=True, cwd=repo, text=True
    )
    return str(done.stdout or "")


# meiva:note
# The range's commits, newest first, as (sha, subject).
def commits(repo: Path, rev_range: str) -> tuple[tuple[str, str], ...]:
    lines = _git(repo, "log", "--format=%H %s", rev_range).splitlines()
    return tuple((line.split(" ", 1)[0], line.split(" ", 1)[1]) for line in lines if line)


# meiva:note
# Whether the commit pays recorded debt down, read from its own manifest diff alone.
def reduces_debt(repo: Path, sha: str) -> bool:
    return manifest_delta(_git(repo, "show", "--format=", "--numstat", sha, "--", MANIFESTS)) > 0


def hunks(repo: Path, sha: str) -> tuple[Hunk, ...]:
    return parse_diff(_git(repo, "show", "--format=", "--unified=0", sha, "--", "*.py"))


# meiva:note
# The files the commit leaves in its tree, so a deleted module's own hunks are skipped.
def existing_paths(repo: Path, sha: str) -> frozenset[str]:
    return frozenset(_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())


# meiva:note
# Every line of that commit's tree that carries one of `messages`, empty when none does: one
# search answers both "did the refusal move somewhere else" and the deleted-module case.
def surviving_text(repo: Path, sha: str, messages: tuple[str, ...]) -> str:
    patterns: list[str] = []
    for message in messages:
        patterns.extend(["-e", message])
    if not patterns:
        return ""
    return _git(repo, "grep", "-F", "-n", *patterns, sha, "--", "*.py", "*.md")


# meiva:note
# Findings over the debt-reducing commits in the range, and how many commits were read - the
# count is what tells an operator the check ran over something rather than nothing.
def scan(repo: Path, rev_range: str, *, rule: str = UNREPLACED) -> tuple[tuple[Finding, ...], int]:
    found: list[Finding] = []
    read = 0
    for sha, subject in commits(repo, rev_range):
        if not reduces_debt(repo, sha):
            continue
        read += 1
        commit_hunks = hunks(repo, sha)
        messages = tuple(message for hunk in commit_hunks for message in refusal_messages(hunk))
        tree = surviving_text(repo, sha, messages)
        found.extend(
            findings_for(
                commit=sha[:10],
                subject=subject,
                hunks=commit_hunks,
                tree_text=tree,
                existing=existing_paths(repo, sha),
                rule=rule,
            )
        )
    return tuple(found), read


def render(found: tuple[Finding, ...]) -> str:
    lines = [summary_line(found)]
    for finding in found:
        mark = "BLOCKING" if finding.blocking else "listed  "
        lines.append(f"{mark} {finding.lens:16s} {finding.commit} {finding.path}")
        lines.append(f"    {finding.detail}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Report a debt-reducing change that removes a refusal instead of moving it."
    )
    parser.add_argument("--base", default="origin/main", help="the commit a pull request targets")
    parser.add_argument("--range", dest="rev_range", help="an explicit git revision range")
    parser.add_argument("--since", help="a history window instead of a pull request")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--blocking",
        choices=(UNREPLACED, DELETION),
        default=DELETION,
        help="which removed refusal fails the run (default: a hunk that adds nothing)",
    )
    parser.add_argument(
        "--no-block", action="store_true", help="report only; never fail the caller"
    )
    args = parser.parse_args(argv)
    rev_range = args.rev_range or (
        f"--since={args.since} HEAD" if args.since else f"{args.base}...HEAD"
    )
    found, read = scan(Path(), rev_range, rule=args.blocking)
    if args.json:
        print(json.dumps([asdict(finding) for finding in found], indent=1))
        return 1 if blocking(found) and not args.no_block else 0
    if not read:
        print(f"refusal-loss: no recorded debt reduction in {rev_range}; nothing to check")
        return 0
    print(render(found))
    print(f"read {read} debt-reducing commit(s) in {rev_range}")
    return 1 if blocking(found) and not args.no_block else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
