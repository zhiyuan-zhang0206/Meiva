from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.options import options
from base.paths.home import resolve_meiva_home
from meiva_builtins.plugins.meiva_memory.pool_ops import (
    RESERVED,
    collect,
    pool_dir,
    untracked_notes,
)
from meiva_builtins.plugins.meiva_memory.template.validate import (
    body_links,
    note_parts,
    resolve_link,
    source_repos,
)

_LEGACY_FIELDS = ("type", "meiva_agent", "meiva_machine", "tags", "timestamp", "generated")
_CONFLICT_MARKER = "<<<<<<< "
_COMMIT_PREFIX = "__COMMIT__"
_IGNORED_GIT_CHANGED_TARGETS = frozenset(
    {"pyproject.toml", "meizel.toml", "meizel.policy.toml", ".pre-commit-config.yaml"}
)


@dataclass(frozen=True)
class NoteRecord:
    path: str
    pure_markdown: bool
    description: str
    sources: tuple[str, ...]
    verified: _dt.date | None
    stale_after: _dt.date | None
    legacy_fields: tuple[str, ...]
    has_conflict_marker: bool


def _parse_date(value: object) -> _dt.date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return _dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _string_list(value: object) -> list[str]:
    if not is_object_list(value):
        return []
    return [item.strip() for item in value if isinstance(item, str)]


def _read_note_record(pool: Path, rel: str) -> NoteRecord | None:
    path = pool / rel
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    pure_md = not text.startswith("---\n")
    parts = note_parts(text)
    fm = parts[0] if parts is not None else {}
    if "sources" in fm:
        raise ValueError(f"{path}: frontmatter sources is forbidden; use inline Markdown links")
    desc_val = fm.get("description")
    desc = desc_val.strip() if isinstance(desc_val, str) else ""
    legacy = tuple(k for k in _LEGACY_FIELDS if k in fm)
    return NoteRecord(
        path=rel,
        pure_markdown=pure_md,
        description=desc,
        sources=tuple(target for _, target in body_links(text)),
        verified=_parse_date(fm.get("verified")),
        stale_after=_parse_date(fm.get("stale_after")),
        legacy_fields=legacy,
        has_conflict_marker=_CONFLICT_MARKER in text,
    )


def _all_pool_notes(pool: Path) -> list[str]:
    return sorted(set(collect(pool)) | set(untracked_notes(pool)))


def _record_touch(
    touches: dict[str, tuple[_dt.date, str, int]],
    rel: str,
    commit_date: _dt.date,
    commit_sha: str,
) -> None:
    existing = touches.get(rel)
    if existing is None:
        touches[rel] = (commit_date, commit_sha, 1)
        return
    prev_date, prev_sha, count = existing
    if commit_date > prev_date:
        touches[rel] = (commit_date, commit_sha, count + 1)
    else:
        touches[rel] = (prev_date, prev_sha, count + 1)


def _repo_touches(repo: Path, since: _dt.date) -> dict[str, tuple[_dt.date, str, int]]:
    res = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            f"--since={since.isoformat()}",
            "--date=short",
            f"--pretty=format:{_COMMIT_PREFIX}%h|%ad",
            "--name-only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return {}
    touches: dict[str, tuple[_dt.date, str, int]] = {}
    current_sha = ""
    current_date: _dt.date | None = None
    for raw in res.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_COMMIT_PREFIX):
            header = line[len(_COMMIT_PREFIX) :]
            sha_part, _, date_part = header.partition("|")
            current_sha = sha_part
            current_date = _parse_date(date_part)
            continue
        if current_date is not None and current_sha:
            _record_touch(touches, line, current_date, current_sha)
    return touches


def _matching_touch(
    touches: Mapping[str, tuple[_dt.date, str, int]], rel_path: str
) -> tuple[_dt.date, str] | None:
    norm = rel_path.rstrip("/")
    prefix = norm + "/"
    best: tuple[_dt.date, str] | None = None
    for path, (dt_val, sha, _count) in touches.items():
        if (path == norm or path.startswith(prefix)) and (best is None or dt_val > best[0]):
            best = (dt_val, sha)
    return best


def _check_file_source(
    note: NoteRecord,
    source: str,
    repos: Mapping[str, Path],
    touches_by_repo: Mapping[str, Mapping[str, tuple[_dt.date, str, int]]],
    since_cutoff: _dt.date,
) -> dict[str, str] | None:
    vault = repos["vault"]
    try:
        target, repo = resolve_link(vault / "memory" / note.path, vault, source, repos)
    except ValueError as exc:
        return {"note": note.path, "source": source, "reason": str(exc), "detail": source}
    rel_target = target.relative_to(repo).as_posix()
    if rel_target in _IGNORED_GIT_CHANGED_TARGETS:
        return None
    touch = _matching_touch(touches_by_repo.get(str(repo), {}), rel_target)
    if touch is None:
        return None
    latest_date, latest_sha = touch
    if latest_date <= note.verified if note.verified is not None else latest_date < since_cutoff:
        return None
    return {
        "note": note.path,
        "source": source,
        "reason": "git_changed",
        "detail": f"{latest_sha} on {latest_date.isoformat()}",
    }


def _missing_okf_entry(note: NoteRecord) -> dict[str, object] | None:
    missing: list[str] = []
    if not note.description:
        missing.append("description")
    if note.verified is None:
        missing.append("verified")
    if not note.sources and Path(note.path).name not in {"MEMORY.md", "INHERIT.md", "index.md"}:
        missing.append("sources")
    if not missing and not note.legacy_fields:
        return None
    return {
        "note": note.path,
        "pure_markdown": note.pure_markdown,
        "missing_fields": missing,
        "legacy_fields": list(note.legacy_fields),
    }


def _pool_dirty_notes(pool: Path) -> list[str]:
    res = subprocess.run(
        ["git", "-C", str(pool), "status", "--porcelain", "--", "*.md"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return []
    dirty: list[str] = []
    for raw in res.stdout.splitlines():
        if len(raw) < 4:
            continue
        rel = raw[3:].strip().strip('"')
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip()
        if rel.endswith(".md") and Path(rel).name not in RESERVED:
            dirty.append(rel)
    return sorted(set(dirty))


def _pool_churn_hotspots(
    pool: Path,
    pool_touches: Mapping[str, tuple[_dt.date, str, int]],
) -> list[dict[str, object]]:
    rows: list[tuple[int, str, str, str]] = []
    for rel, (dt_val, sha, count) in pool_touches.items():
        if (
            count < 2
            or not rel.endswith(".md")
            or Path(rel).name in RESERVED
            or not (pool / rel).is_file()
        ):
            continue
        rows.append((-count, rel, sha, dt_val.isoformat()))
    rows.sort()
    return [
        {"note": rel, "commits": -neg_count, "latest_commit": sha, "latest_date": dt_str}
        for neg_count, rel, sha, dt_str in rows
    ]


def _workspace_memory_candidates(home: Path, since_cutoff: _dt.date) -> list[dict[str, str]]:
    workspaces = home / "workspaces"
    if not workspaces.is_dir():
        return []
    candidates: list[dict[str, str]] = []
    paths = sorted({*workspaces.glob("*/memory/*.md"), *workspaces.glob("*/memory/inherit/*.md")})
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size <= 0:
            continue
        mod_date = _dt.datetime.fromtimestamp(st.st_mtime, tz=_dt.UTC).date()
        if mod_date < since_cutoff:
            continue
        rel = path.relative_to(workspaces).as_posix()
        agent_id = rel.split("/", 1)[0]
        candidates.append({"agent_id": agent_id, "path": rel, "modified": mod_date.isoformat()})
    return candidates


def _scan_note_sources(
    records: Sequence[NoteRecord],
    repo_map: Mapping[str, Path],
    touches_by_repo: Mapping[str, Mapping[str, tuple[_dt.date, str, int]]],
    since_cutoff: _dt.date,
) -> list[dict[str, str]]:
    source_drift: list[dict[str, str]] = []
    for rec in records:
        for src in rec.sources:
            drift = _check_file_source(rec, src, repo_map, touches_by_repo, since_cutoff)
            if drift is not None:
                source_drift.append(drift)
    return source_drift


def _expired_entries(records: Sequence[NoteRecord], today: _dt.date) -> list[dict[str, str]]:
    return [
        {"note": rec.path, "stale_after": rec.stale_after.isoformat()}
        for rec in records
        if rec.stale_after is not None and rec.stale_after <= today
    ]


def _aging_entries(
    records: Sequence[NoteRecord], today: _dt.date, max_verified_age_days: int
) -> list[dict[str, object]]:
    return [
        {
            "note": rec.path,
            "verified": rec.verified.isoformat(),
            "age_days": (today - rec.verified).days,
        }
        for rec in records
        if rec.verified is not None and (today - rec.verified).days > max_verified_age_days
    ]


# meiva:note
# Deterministic Phase 2 (`Gather Signal`) probe for Meiva Dream consolidation: scans every note in
# `pool`, intersects inline Markdown file links against Vault and mapped repository file trees
# and recent git history, and surfaces expired `stale_after` notes, aging `verified` dates, notes
# lacking Dream OKF fields (including newly written pure-Markdown notes), and pool churn/conflict
# hotspots.
def gather_signals(
    pool: Path,
    *,
    home: Path | None = None,
    repos: Mapping[str, Path] | None = None,
    today: _dt.date,
    since_days: int = 7,
    max_verified_age_days: int = 30,
) -> dict[str, object]:
    resolved_home = home if home is not None else resolve_meiva_home()
    vault = pool.parent if pool.name == "memory" else pool
    repo_map: dict[str, Path] = {
        **(dict(repos) if repos is not None else {}),
        "vault": vault.resolve(),
    }
    repo_map = {**source_repos(vault, repo_map), "vault": vault.resolve()}
    records = [
        rec for rel in _all_pool_notes(pool) if (rec := _read_note_record(pool, rel)) is not None
    ]
    since_cutoff = today - _dt.timedelta(days=max(since_days, 1))
    earliest = min([since_cutoff, *(rec.verified for rec in records if rec.verified is not None)])
    touches_by_repo = {
        str(root.resolve()): _repo_touches(root, earliest)
        for root in repo_map.values()
        if root.is_dir()
    }
    pool_window_touches = _repo_touches(pool, since_cutoff) if pool.is_dir() else {}
    source_drift = _scan_note_sources(records, repo_map, touches_by_repo, since_cutoff)
    missing_okf = [gap for rec in records if (gap := _missing_okf_entry(rec)) is not None]
    conflict_notes = [rec.path for rec in records if rec.has_conflict_marker]
    return {
        "pool": str(pool),
        "today": today.isoformat(),
        "total_notes": len(records),
        "source_drift": source_drift,
        "expired_notes": _expired_entries(records, today),
        "aging_verified_notes": _aging_entries(records, today, max_verified_age_days),
        "missing_okf_notes": missing_okf,
        "workspace_memory_candidates": _workspace_memory_candidates(resolved_home, since_cutoff),
        "pool_hotspots": {
            "churn_hotspots": _pool_churn_hotspots(pool, pool_window_touches),
            "conflict_markers": conflict_notes,
            "uncommitted_notes": _pool_dirty_notes(pool),
        },
    }


def _dict_items(value: object) -> list[dict[str, object]]:
    if not is_object_list(value):
        return []
    return [item for item in value if is_object_dict(item)]


def _append_drift_section(lines: list[str], report: Mapping[str, object]) -> None:
    drifts = _dict_items(report.get("source_drift"))
    lines.append(f"## Source Drift ({len(drifts)})")
    if not drifts:
        lines.append("- (none)")
    for row in drifts:
        lines.append(
            f"- `{row.get('note')}` -> `{row.get('source')}` [{row.get('reason')}]: {row.get('detail')}"
        )


def _append_workspace_section(lines: list[str], report: Mapping[str, object]) -> None:
    candidates = _dict_items(report.get("workspace_memory_candidates"))
    lines.append(f"\n## Workspace Memory Candidates ({len(candidates)})")
    if not candidates:
        lines.append("- (none)")
        return
    for row in candidates:
        lines.append(
            f"- agent `{row.get('agent_id')}`: `{row.get('path')}` (modified: {row.get('modified')})"
        )


_PREVIEW_CAP = 20


def _append_lifecycle_sections(lines: list[str], report: Mapping[str, object]) -> None:
    expired = _dict_items(report.get("expired_notes"))
    lines.append(f"\n## Expired Notes ({len(expired)})")
    if not expired:
        lines.append("- (none)")
    for row in expired:
        lines.append(f"- `{row.get('note')}` (stale_after: {row.get('stale_after')})")

    aging = _dict_items(report.get("aging_verified_notes"))
    lines.append(f"\n## Aging Verified Notes ({len(aging)})")
    if not aging:
        lines.append("- (none)")
    for row in aging:
        lines.append(
            f"- `{row.get('note')}` (verified: {row.get('verified')}, {row.get('age_days')}d old)"
        )

    missing = _dict_items(report.get("missing_okf_notes"))
    lines.append(f"\n## Notes Needing Dream OKF Enrichment ({len(missing)})")
    if not missing:
        lines.append("- (none)")
    for row in missing[:_PREVIEW_CAP]:
        kind = "pure-md" if row.get("pure_markdown") else "frontmatter"
        lines.append(
            f"- `{row.get('note')}` ({kind}) missing={row.get('missing_fields')} legacy={row.get('legacy_fields')}"
        )
    if len(missing) > _PREVIEW_CAP:
        lines.append(f"- ... and {len(missing) - _PREVIEW_CAP} more (use --json for full list)")


def _append_hotspot_section(lines: list[str], report: Mapping[str, object]) -> None:
    hotspots = report.get("pool_hotspots")
    if not is_object_dict(hotspots):
        return
    churn = _dict_items(hotspots.get("churn_hotspots"))
    conflicts = _string_list(hotspots.get("conflict_markers"))
    uncommitted = _string_list(hotspots.get("uncommitted_notes"))
    lines.append(
        f"\n## Pool Hotspots (churn={len(churn)}, conflicts={len(conflicts)}, uncommitted={len(uncommitted)})"
    )
    for row in churn:
        lines.append(
            f"- churn: `{row.get('note')}` ({row.get('commits')} commits, latest {row.get('latest_commit')})"
        )
    for path in conflicts:
        lines.append(f"- conflict_marker: `{path}`")
    for path in uncommitted:
        lines.append(f"- uncommitted: `{path}`")


def render_report(report: Mapping[str, object]) -> str:
    lines = [
        f"# Meiva Dream Phase 2 Signal Report ({report.get('today')})",
        f"Pool: `{report.get('pool')}` | Notes scanned: {report.get('total_notes')}",
        "",
    ]
    _append_drift_section(lines, report)
    _append_hotspot_section(lines, report)
    _append_lifecycle_sections(lines, report)
    _append_workspace_section(lines, report)
    return "\n".join(lines) + "\n"


def _parse_repo_specs(specs: Sequence[str]) -> dict[str, Path]:
    repos: dict[str, Path] = {}
    for spec in specs:
        alias, sep, raw_path = spec.partition("=")
        if not sep or not alias.strip() or not raw_path.strip():
            raise SystemExit(f"Invalid --repo specification (expected name=/path): {spec!r}")
        repos[alias.strip()] = Path(raw_path.strip()).expanduser().resolve()
    return repos


@dataclass(frozen=True)
class _Arguments:
    pool: str | None
    repo: list[str]
    today: str | None
    since_days: int
    max_verified_age_days: int
    json_output: bool


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Gather deterministic Phase 2 World Truth and lifecycle signals for Meiva Dream"
    )
    _ = ap.add_argument("--pool", default=None, help="Path to the memory pool or worktree")
    _ = ap.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repeatable alias=/path mapping for /repos/<alias>/<path> sources",
    )
    _ = ap.add_argument("--today", default=None, help="Override today's ISO date (YYYY-MM-DD)")
    _ = ap.add_argument(
        "--since-days",
        type=int,
        default=7,
        help="Git log lookback window in days for unverified notes and pool churn hotspots",
    )
    _ = ap.add_argument(
        "--max-verified-age-days",
        type=int,
        default=30,
        help="Flag notes whose verified date is older than this many days",
    )
    _ = ap.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Print structured JSON report instead of Markdown",
    )
    args = options(ap.parse_args(argv), _Arguments)
    home = resolve_meiva_home()
    pool = Path(args.pool).expanduser().resolve() if args.pool else pool_dir(home)
    today = _parse_date(args.today) if args.today else _dt.datetime.now(_dt.UTC).date()
    if today is None:
        raise SystemExit(f"Invalid --today date (expected YYYY-MM-DD): {args.today!r}")
    repos = _parse_repo_specs(args.repo)
    report = gather_signals(
        pool,
        home=home,
        repos=repos,
        today=today,
        since_days=args.since_days,
        max_verified_age_days=args.max_verified_age_days,
    )
    try:
        if args.json_output:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(render_report(report), end="")
        _ = sys.stdout.flush()
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
