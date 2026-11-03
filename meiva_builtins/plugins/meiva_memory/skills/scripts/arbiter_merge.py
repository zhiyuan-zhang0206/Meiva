# meiva:note
# DORMANT ON PURPOSE (2026-11-04, task #6157): nothing in this tree invokes this script —
# the 04:00 schedule only wakes the memory-arbiter agent (`wake_arbiter` ->
# `ensure_agent`), and the agent merges by hand under its own rules. The script is kept
# safe for the day someone wires it up: it validates every PR it would merge and, unless
# invoked with --apply, it merges nothing at all.
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.paths.home import resolve_meiva_home
from meiva_builtins.plugins.meiva_memory.pool_ops import pool_dir, refresh_index, repo_slug, run

# meiva:note
# The pool gate is the `validate` check; a PR without it is never merged (task #6157).
VALIDATE_CHECK = "validate"
SNAPSHOT_HEADING = "## Collector snapshot (taken before this merge)"
FACES_FILE = "memory-faces.txt"


# meiva:note
# None means the checks could not be read; an empty list means there are none. That
# difference is what turns into UNREADABLE below.
def _decoded(text: str) -> object:
    try:
        value: object = json.loads(text or "{}")
    except json.JSONDecodeError:
        return None
    return value


def _first_text(row: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _checks(pr: int, slug: str) -> list[tuple[str, str]] | None:
    r = run(
        ["gh", "pr", "view", str(pr), "--repo", slug, "--json", "statusCheckRollup"],
        check=False,
    )
    if r.returncode != 0:
        return None
    payload = _decoded(r.stdout)
    if not is_object_dict(payload):
        return None
    rows: list[tuple[str, str]] = []
    rollup = payload.get("statusCheckRollup")
    if not is_object_list(rollup):
        return rows
    for check in rollup:
        if not is_object_dict(check):
            continue
        rows.append(
            (
                _first_text(check, "name", "context") or "?",
                _first_text(check, "conclusion", "state", "status").upper(),
            )
        )
    return rows


# meiva:note
# Only a validate that concluded SUCCESS is GREEN. Unreadable, absent, pending and failed
# all keep the PR out: a pool whose gate cannot be read is not a pool that passed the gate.
def validate_verdict(rows: list[tuple[str, str]] | None) -> str:
    if rows is None:
        return "UNREADABLE"
    for name, state in rows:
        if name == VALIDATE_CHECK:
            if state == "SUCCESS":
                return "GREEN"
            return state or "UNKNOWN"
    return "ABSENT"


def _git(pool: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(pool), *args], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else "?"


def _dirty(pool: Path) -> str:
    status = _git(pool, "status", "--porcelain")
    return "?" if status == "?" else str(len([ln for ln in status.splitlines() if ln.strip()]))


# meiva:note
# The operator's optional face list: `<pool>/../memory-faces.txt`, one `name<TAB>ssh-target`
# row per remote checkout.
def _faces(pool: Path) -> list[tuple[str, str]]:
    listing = pool.parent / FACES_FILE
    if not listing.is_file():
        return []
    rows: list[tuple[str, str]] = []
    for raw in listing.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


# meiva:note
# Every collision on 2026-11-03/04 was "nobody could say which tree the collector was
# looking at" (task #6157). Recording HEAD, dirty count and behind-count before a merge
# turns that into an auditable fact, and an unreachable face is written as unreachable so
# the absence stays visible instead of passing silently. The collector row carries a fixed
# label because the host name is ambient here and it is not the fact being audited.
def participation_snapshot(pool: Path) -> str:
    lines = [
        SNAPSHOT_HEADING,
        "",
        "| face | HEAD | dirty | behind origin/main |",
        "|---|---|---|---|",
    ]
    head = _git(pool, "rev-parse", "--short", "HEAD")
    behind = _git(pool, "rev-list", "--count", "HEAD..origin/main")
    lines.append(f"| collector | {head} | {_dirty(pool)} | {behind} |")
    faces = _faces(pool)
    if not faces:
        lines.append(f"| (other faces) | not configured | — | see {FACES_FILE} |")
    for name, target in faces:
        probe = (
            "cd ~/.meiva/memory && git rev-parse --short HEAD && "
            "git status --porcelain | wc -l && git rev-list --count HEAD..origin/main"
        )
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", target, probe],
            capture_output=True,
            text=True,
            check=False,
        )
        parts = r.stdout.split()
        if r.returncode != 0 or len(parts) < 3:
            lines.append(f"| {name} | UNREACHABLE | — | — |")
            continue
        lines.append(f"| {name} | {parts[0]} | {parts[1]} | {parts[2]} |")
    return "\n".join(lines)


def _write_snapshot(pr: int, slug: str, snapshot: str) -> bool:
    r = run(["gh", "pr", "view", str(pr), "--repo", slug, "--json", "body"], check=False)
    if r.returncode != 0:
        return False
    payload = _decoded(r.stdout)
    if not is_object_dict(payload):
        return False
    raw_body = payload.get("body")
    body = raw_body if isinstance(raw_body, str) else ""
    kept = (
        body.split(SNAPSHOT_HEADING, 1)[0].rstrip() if SNAPSHOT_HEADING in body else body.rstrip()
    )
    new_body = f"{kept}\n\n{snapshot}\n" if kept else f"{snapshot}\n"
    r = subprocess.run(
        ["gh", "pr", "edit", str(pr), "--repo", slug, "--body-file", "-"],
        input=new_body,
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Merge pool PRs whose pool gate is green (dormant: pass --apply to act)"
    )
    _ = ap.add_argument(
        "--apply",
        action="store_true",
        help="actually merge; without it the run only reports what it would merge",
    )
    apply_flag: object = ap.parse_args().apply
    apply_changes = apply_flag is True

    pool = pool_dir(resolve_meiva_home())
    slug = repo_slug(pool)

    listed = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--base",
            "main",
            "--json",
            "number",
            "-q",
            ".[].number",
        ],
        check=False,
    )
    numbers = [int(n) for n in listed.stdout.split()]
    if not numbers:
        print("  (no open PRs)")
        return 0

    snapshot = participation_snapshot(pool)
    merged: list[int] = []
    skipped: list[tuple[int, str]] = []
    for n in numbers:
        verdict = validate_verdict(_checks(n, slug))
        if verdict != "GREEN":
            skipped.append((n, f"validate {verdict}"))
            print(f"  ✗ PR #{n} skipped: validate {verdict}", file=sys.stderr)
            continue
        if not apply_changes:
            print(f"  would merge PR #{n} (validate GREEN) — dry run, pass --apply to merge")
            continue
        if not _write_snapshot(n, slug, snapshot):
            skipped.append((n, "could not write the participation snapshot"))
            print(f"  ✗ PR #{n} skipped: snapshot write failed", file=sys.stderr)
            continue
        r = run(["gh", "pr", "merge", str(n), "--repo", slug, "--squash"], check=False)
        if r.returncode == 0:
            merged.append(n)
            print(f"  merged PR #{n}")
        else:
            reason = r.stderr.strip() or str(r.returncode)
            skipped.append((n, reason))
            print(f"  ✗ PR #{n} merge failed: {reason}", file=sys.stderr)

    verb = "merged" if apply_changes else "would merge (dry run)"
    print(
        f"\n  {verb}: {len(merged) if apply_changes else len(numbers) - len(skipped)}  "
        f"skipped: {len(skipped)}"
    )
    rc = 1 if skipped else 0

    if merged:
        try:
            refresh_index()
        except (SystemExit, OSError):
            print(
                "  ✗ post-merge refresh FAILED — the index is stale until it succeeds; "
                "run `meiva memory refresh` manually once the gateway is reachable",
                file=sys.stderr,
            )
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
