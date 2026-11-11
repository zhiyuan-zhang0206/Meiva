from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from meiva_builtins.plugins.meiva_memory.skills.consolidation.scripts import workspace_memory as wm

TODAY = dt.date(2026, 11, 11)


def _note(
    home: Path,
    agent: str = "1",
    name: str = "rule.md",
    text: str = "# Retry budget\n\nRetries need a bounded budget.",
) -> Path:
    note = home / "workspaces" / agent / "memory" / name
    note.parent.mkdir(parents=True, exist_ok=True)
    _ = note.write_text(text, encoding="utf-8")
    return note


def test_hash_ledger_recovers_old_notes_and_skips_reviewed_content(tmp_path: Path) -> None:
    note = _note(tmp_path)
    os.utime(note, (1, 1))
    rows = wm.collect_candidates(tmp_path, "mini")
    ledger = tmp_path / "ledger.json"
    assert wm.filter_candidates(rows, ledger, TODAY, False) == ([], 0)
    pending, skipped = wm.filter_candidates(rows, ledger, TODAY, True)
    assert pending[0]["status"] == "unreviewed"
    assert skipped == 0
    wm.record_review(
        tmp_path,
        ledger,
        "mini:1/memory/rule.md",
        "local_only",
        None,
        TODAY,
        False,
        "mini",
        tmp_path,
    )
    assert wm.filter_candidates(rows, ledger, TODAY, False) == ([], 1)
    _ = note.write_text("# Retry budget\n\nUse at most three retries.")
    changed, skipped = wm.filter_candidates(
        wm.collect_candidates(tmp_path, "mini"), ledger, TODAY, False
    )
    assert changed[0]["status"] == "modified_since_review"
    assert skipped == 0


def test_legacy_keys_empty_notes_and_inherit_details(tmp_path: Path) -> None:
    _ = _note(tmp_path, name="empty.md", text=" \n")
    _ = _note(tmp_path, name="inherit/rule.md")
    rows = wm.collect_candidates(tmp_path, "mini")
    assert len(rows) == 1
    ledger = tmp_path / "ledger.json"
    _ = ledger.write_text(
        json.dumps(
            {"entries": {rows[0]["path"]: {"sha256": rows[0]["sha256"], "status": "local_only"}}}
        )
    )
    assert wm.filter_candidates(rows, ledger, TODAY, False) == ([], 1)


def test_remote_merge_is_deterministic_and_rejects_conflicts(tmp_path: Path) -> None:
    _ = _note(tmp_path)
    local = wm.collect_candidates(tmp_path, "mini")
    remote = [{**local[0], "machine": "air", "agent_id": "2", "path": "2/memory/rule.md"}]
    export = tmp_path / "remote.json"
    _ = export.write_text(json.dumps(remote))
    merged = wm.merge_candidates(local, [export, export])
    assert [r["machine"] for r in merged] == ["air", "mini"]
    _ = export.write_text(json.dumps([{**local[0], "sha256": "a" * 64}]))
    with pytest.raises(ValueError, match="Conflicting"):
        _ = wm.merge_candidates(local, [export])
    _ = export.write_text(json.dumps([{**remote[0], "path": "2/memory/../../secret.md"}]))
    with pytest.raises(ValueError, match="Invalid workspace"):
        _ = wm.merge_candidates(local, [export])


def test_scope_traps_require_distinct_agents_and_low_pool_coverage(tmp_path: Path) -> None:
    _ = _note(tmp_path, "1")
    _ = _note(tmp_path, "2")
    rows = wm.collect_candidates(tmp_path, "mini")
    assert len(wm.scope_traps(rows, [], None)) == 2
    assert wm.scope_traps(rows, ["# Retry budget\nRetries need a bounded budget."], None) == []
    assert wm.scope_traps([rows[0], {**rows[0], "path": "1/memory/other.md"}], [], None) == []
    assert wm.scope_traps([rows[0]], [], None) == []


def test_cross_agent_demand_and_standing_rules(tmp_path: Path) -> None:
    _ = _note(tmp_path)
    rows = wm.collect_candidates(tmp_path, "mini")
    demands = tmp_path / "demand.jsonl"
    _ = demands.write_text(
        json.dumps({"text": "# Retry budget\nbounded retries", "agent_id": "2"}) + "\n"
    )
    assert wm.scope_traps(rows, [], demands)[0]["demand_match"] is True
    _ = demands.write_text(json.dumps({"text": "# Retry budget", "agent_id": "1"}) + "\n")
    assert wm.scope_traps(rows, [], demands) == []
    _ = demands.write_text(json.dumps({"text": "# Retry budget", "kind": "platform-rule"}) + "\n")
    assert len(wm.scope_traps(rows, [], demands)) == 1


def test_prune_updates_index_and_keeps_referenced_pointer(tmp_path: Path) -> None:
    note = _note(tmp_path)
    index = _note(tmp_path, name="MEMORY.md", text="- [Rule](rule.md)\n")
    _ = _note(tmp_path, name="INHERIT.md", text="Read [rule](rule.md).")
    pool = tmp_path / "vault" / "memory"
    pool.mkdir(parents=True)
    _ = (pool / "retry.md").write_text("# Retry budget")
    ledger = tmp_path / "ledger.json"
    wm.record_review(
        tmp_path, ledger, "mini:1/memory/rule.md", "promoted", "retry.md", TODAY, True, "mini", pool
    )
    assert index.read_text() == "- [Rule](~/.meiva/vault/memory/retry.md)\n"
    assert note.read_text() == "[Shared memory](~/.meiva/vault/memory/retry.md)\n"
    assert '"status": "pruned"' in ledger.read_text()


def test_unreferenced_note_is_removed_and_bad_reviews_do_not_mutate(tmp_path: Path) -> None:
    note = _note(tmp_path)
    pool = tmp_path / "pool"
    pool.mkdir()
    _ = (pool / "rule.md").write_text("# Retry budget")
    ledger = tmp_path / "ledger.json"
    for key, status, promoted in [
        ("air:1/memory/rule.md", "promoted", "rule.md"),
        ("mini:1/memory/../../x.md", "promoted", "rule.md"),
        ("mini:1/memory/rule.md", "unknown", None),
        ("mini:1/memory/rule.md", "promoted", "../rule.md"),
        ("mini:1/memory/rule.md", "promoted", "missing.md"),
    ]:
        with pytest.raises(ValueError):
            wm.record_review(tmp_path, ledger, key, status, promoted, TODAY, True, "mini", pool)
        assert note.exists()
        assert not ledger.exists()
    wm.record_review(
        tmp_path, ledger, "1/memory/rule.md", "promoted", "rule.md", TODAY, True, "mini", pool
    )
    assert not note.exists()
    assert "mini:1/memory/rule.md" in ledger.read_text()


def test_pruned_pointer_is_already_reviewed(tmp_path: Path) -> None:
    _ = _note(tmp_path)
    _ = _note(tmp_path, name="INHERIT.md", text="[Rule](rule.md)")
    pool = tmp_path / "pool"
    pool.mkdir()
    _ = (pool / "retry.md").write_text("# Retry budget")
    ledger = tmp_path / "ledger.json"
    wm.record_review(
        tmp_path, ledger, "mini:1/memory/rule.md", "promoted", "retry.md", TODAY, True, "mini", pool
    )
    pending, skipped = wm.filter_candidates(
        wm.collect_candidates(tmp_path, "mini"), ledger, TODAY, True
    )
    assert skipped == 1
    assert [row["path"] for row in pending] == ["1/memory/INHERIT.md"]


def test_prune_rejects_symlink_index_and_cross_agent_detail(tmp_path: Path) -> None:
    note = _note(tmp_path)
    other = _note(tmp_path, "2")
    pool = tmp_path / "pool"
    pool.mkdir()
    _ = (pool / "retry.md").write_text("# Retry budget")
    index = note.parent / "MEMORY.md"
    index.symlink_to(other)
    ledger = tmp_path / "ledger.json"
    with pytest.raises(ValueError, match="symlink"):
        wm.record_review(
            tmp_path,
            ledger,
            "mini:1/memory/rule.md",
            "promoted",
            "retry.md",
            TODAY,
            True,
            "mini",
            pool,
        )
    assert note.exists()
    assert not ledger.exists()
    index.unlink()
    link = note.parent / "link.md"
    link.symlink_to(other)
    with pytest.raises(ValueError, match="escapes"):
        wm.record_review(
            tmp_path,
            ledger,
            "mini:1/memory/link.md",
            "promoted",
            "retry.md",
            TODAY,
            True,
            "mini",
            pool,
        )
    assert other.exists()
