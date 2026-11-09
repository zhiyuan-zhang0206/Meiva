from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

import pytest

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.proc import run_bounded
from meiva_builtins.plugins.meiva_memory.skills.consolidation.scripts import dream_gather


def _git_commit(repo: Path, rel: str, content: str, iso_date: str, message: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_text(content, encoding="utf-8")
    add_res = run_bounded(["git", "-C", str(repo), "add", rel], capture_output=True, timeout=30)
    assert add_res.returncode == 0
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": f"{iso_date}T12:00:00+00:00",
        "GIT_COMMITTER_DATE": f"{iso_date}T12:00:00+00:00",
    }
    commit_res = run_bounded(
        ["git", "-C", str(repo), "commit", "-m", message],
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert commit_res.returncode == 0


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    init_res = run_bounded(["git", "-C", str(path), "init", "-q"], capture_output=True, timeout=30)
    assert init_res.returncode == 0
    return path


def _git_rm(repo: Path, rel: str, iso_date: str, message: str) -> None:
    rm_res = run_bounded(["git", "-C", str(repo), "rm", "-q", rel], capture_output=True, timeout=30)
    assert rm_res.returncode == 0
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": f"{iso_date}T12:00:00+00:00",
        "GIT_COMMITTER_DATE": f"{iso_date}T12:00:00+00:00",
    }
    commit_res = run_bounded(
        ["git", "-C", str(repo), "commit", "-m", message],
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert commit_res.returncode == 0


def _seed_pool_and_code_repo(tmp_path: Path) -> dict[str, object]:
    code_repo = _init_git_repo(tmp_path / "meiva")
    _git_commit(code_repo, "pkg/stable.py", "y = 1\n", "2026-08-20", "stable file")
    _git_commit(code_repo, "pkg/mod.py", "x = 1\n", "2026-10-25", "init mod")
    _git_commit(code_repo, "pkg/mod.py", "x = 2\n", "2026-11-05", "update mod after verified")
    _git_commit(code_repo, "pyproject.toml", "[project]\n", "2026-11-06", "manifest touch")

    pool = _init_git_repo(tmp_path / "pool")
    _git_commit(
        pool,
        "expired.md",
        '---\ndescription: "Temporary freeze"\nsources:\n  - "repo:meiva:pkg/stable.py"\n'
        'verified: "2026-09-01"\nstale_after: "2026-11-09"\n---\n# Freeze\n',
        "2026-11-03",
        "add expired note",
    )
    _git_commit(
        pool,
        "legacy.md",
        '---\ntype: Memory\nmeiva_agent: 7\ndescription: "Legacy note"\ntags: [type/user]\n---\n# Legacy\n',
        "2026-11-03",
        "add legacy note",
    )
    canonical_body = (
        '---\ndescription: "Canonical rule"\nsources:\n'
        '  - "repo:meiva:pkg/mod.py"\n'
        '  - "repo:meiva:pkg/stable.py"\n'
        '  - "repo:meiva:pyproject.toml"\n'
        '  - "repo:meiva:pkg/missing.py"\n'
        '  - "repo:monsora:app/main.py"\n'
        '  - "repo:other:foo.py"\n'
        '  - "db:meiva_main:schedules#1"\n'
        '  - "user:ruling"\n'
        '  - "user:preference"\n'
        '  - "env:macmini"\n'
        'verified: "2026-11-01"\n---\n'
    )
    _git_commit(pool, "canonical.md", canonical_body + "# Canonical\n", "2026-11-04", "v1")
    _git_commit(pool, "canonical.md", canonical_body + "# Canonical v2\n", "2026-11-06", "v2")
    _git_commit(pool, "deleted-20261101.md", "# Old v1\n", "2026-11-04", "old v1")
    _git_commit(pool, "deleted-20261101.md", "# Old v2\n", "2026-11-05", "old v2")
    _git_rm(pool, "deleted-20261101.md", "2026-11-06", "delete fragment")
    _ = (pool / "pure-untracked.md").write_text(
        "# Pure Markdown\n\nSummary.\n\n<<<<<<< HEAD\nconflict\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    ws_recent = home / "workspaces" / "405" / "memory" / "MEMORY.md"
    ws_recent.parent.mkdir(parents=True, exist_ok=True)
    _ = ws_recent.write_text("# Agent 405 Memory\n", encoding="utf-8")
    recent_ts = _dt.datetime(2026, 11, 8, 12, 0, tzinfo=_dt.UTC).timestamp()
    os.utime(ws_recent, (recent_ts, recent_ts))

    ws_inherit = home / "workspaces" / "405" / "memory" / "inherit" / "domain.md"
    ws_inherit.parent.mkdir(parents=True, exist_ok=True)
    _ = ws_inherit.write_text("# Inherited Rule\n", encoding="utf-8")
    os.utime(ws_inherit, (recent_ts, recent_ts))

    ws_empty = home / "workspaces" / "405" / "memory" / "empty.md"
    _ = ws_empty.write_text("", encoding="utf-8")
    os.utime(ws_empty, (recent_ts, recent_ts))

    ws_old = home / "workspaces" / "99" / "memory" / "old.md"
    ws_old.parent.mkdir(parents=True, exist_ok=True)
    _ = ws_old.write_text("# Old\n", encoding="utf-8")
    old_ts = _dt.datetime(2026, 10, 1, 12, 0, tzinfo=_dt.UTC).timestamp()
    os.utime(ws_old, (old_ts, old_ts))

    return dream_gather.gather_signals(
        pool,
        home=home,
        repos={"meiva": code_repo},
        today=_dt.date(2026, 11, 9),
        since_days=7,
        max_verified_age_days=30,
    )


def test_gather_signals_detects_source_drift_and_external_probes(tmp_path: Path) -> None:
    report = _seed_pool_and_code_repo(tmp_path)
    assert report["total_notes"] == 4
    drifts = report["source_drift"]
    assert is_object_list(drifts)
    drift_triples = [
        (row.get("note"), row.get("source"), row.get("reason"))
        for row in drifts
        if is_object_dict(row)
    ]
    assert drift_triples == [
        ("canonical.md", "repo:meiva:pkg/mod.py", "git_changed"),
        ("canonical.md", "repo:meiva:pkg/missing.py", "missing_path"),
        ("canonical.md", "repo:other:foo.py", "unmapped_repo"),
    ]
    assert report["external_source_probes"] == [
        {"note": "canonical.md", "source": "db:meiva_main:schedules#1"}
    ]


def test_gather_signals_detects_lifecycle_okf_gaps_and_hotspots(tmp_path: Path) -> None:
    report = _seed_pool_and_code_repo(tmp_path)
    assert report["expired_notes"] == [{"note": "expired.md", "stale_after": "2026-11-09"}]
    assert report["aging_verified_notes"] == [
        {"note": "expired.md", "verified": "2026-09-01", "age_days": 69}
    ]
    assert report["missing_okf_notes"] == [
        {
            "note": "legacy.md",
            "pure_markdown": False,
            "missing_fields": ["verified", "sources"],
            "legacy_fields": ["type", "meiva_agent", "tags"],
        },
        {
            "note": "pure-untracked.md",
            "pure_markdown": True,
            "missing_fields": ["description", "verified", "sources"],
            "legacy_fields": [],
        },
    ]
    hotspots = report["pool_hotspots"]
    assert is_object_dict(hotspots)
    churn = hotspots.get("churn_hotspots")
    assert is_object_list(churn)
    assert [row.get("note") for row in churn if is_object_dict(row)] == ["canonical.md"]
    assert hotspots["conflict_markers"] == ["pure-untracked.md"]
    assert hotspots["uncommitted_notes"] == ["pure-untracked.md"]


def test_gather_signals_detects_workspace_candidates_and_renders_sections(tmp_path: Path) -> None:
    report = _seed_pool_and_code_repo(tmp_path)
    assert report["workspace_memory_candidates"] == [
        {"agent_id": "405", "path": "405/memory/MEMORY.md", "modified": "2026-11-08"},
        {"agent_id": "405", "path": "405/memory/inherit/domain.md", "modified": "2026-11-08"},
    ]
    rendered = dream_gather.render_report(report)
    assert "## External Source Probes (1)" in rendered
    assert "- `canonical.md` -> `db:meiva_main:schedules#1`" in rendered
    assert "## Workspace Memory Candidates (2)" in rendered
    assert "- agent `405`: `405/memory/MEMORY.md` (modified: 2026-11-08)" in rendered
    assert "- agent `405`: `405/memory/inherit/domain.md` (modified: 2026-11-08)" in rendered


def test_dream_gather_cli_json_and_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    _ = (pool / "note.md").write_text("# Pure Note\n\nSummary.\n", encoding="utf-8")

    rc = dream_gather.main(["--pool", str(pool), "--today", "2026-11-09", "--json"])
    assert rc == 0
    payload = object_of(json.loads, capsys.readouterr().out)
    assert is_object_dict(payload)
    assert payload["total_notes"] == 1

    rc_md = dream_gather.main(["--pool", str(pool), "--today", "2026-11-09"])
    assert rc_md == 0
    md_out = capsys.readouterr().out
    assert "# Meiva Dream Phase 2 Signal Report (2026-11-09)" in md_out
    assert "`note.md` (pure-md)" in md_out
