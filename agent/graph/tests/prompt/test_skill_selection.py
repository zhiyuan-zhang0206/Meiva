# meiva:note
# Which skills the prompt surfaces cover, decided from a `SkillView` value. The index
# membership, the drift diff against a snapshot of it, the recall candidates and the drift
# note all read the same view, so a test builds the catalog on disk, scans it with the
# production scanner and hands the result in.
from __future__ import annotations

import shutil
from pathlib import Path

from agent.graph.prompt.capabilities import (
    NEW_SKILLS_MAX_ENTRIES,
    RecallCandidate,
    index_entry,
    new_skills_note_text,
    recall_candidates,
)
from agent.graph.prompt.skill_selection import (
    index_drift,
    indexed_skill_identifiers,
    indexed_skills,
    skill_identifiers,
)
from agent.graph.tests.prompt.skill_support import skill_view, write_skill


def test_drift_reports_only_what_appeared_since_the_snapshot(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(tmp_path))
    assert snapshot == {"alpha"}

    write_skill(tmp_path, "beta", "beta", "Beta desc")
    drift = index_drift(snapshot, skill_view(tmp_path))

    assert [s["name"] for s in drift.added] == ["beta"]
    assert drift.identifiers == {"alpha", "beta"}


def test_drift_is_empty_when_the_catalog_has_not_moved(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", "alpha", "Alpha desc")
    view = skill_view(tmp_path)

    assert index_drift(indexed_skill_identifiers(view), view).added == []


def test_a_removed_and_restored_skill_is_announced_again(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(tmp_path))

    shutil.rmtree(tmp_path / "alpha")
    after_removal = index_drift(snapshot, skill_view(tmp_path))
    assert after_removal.added == []
    assert after_removal.identifiers == set()

    write_skill(tmp_path, "alpha", "alpha", "Alpha desc")
    restored = index_drift(after_removal.identifiers, skill_view(tmp_path))
    assert [s["name"] for s in restored.added] == ["alpha"]


def test_narrowing_holds_and_a_configured_name_that_arrives_late_drifts_in(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(tmp_path, inject=["beta"]))
    assert snapshot == set()

    write_skill(tmp_path, "beta", "beta", "Beta desc")
    write_skill(tmp_path, "gamma", "gamma", "Gamma desc")
    drift = index_drift(snapshot, skill_view(tmp_path, inject=["beta"]))

    assert [s["name"] for s in drift.added] == ["beta"]
    assert drift.identifiers == {"beta"}


def test_a_disabled_skills_surface_has_no_membership(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", "alpha", "Alpha desc")
    view = skill_view(tmp_path, disabled=["skills"])

    assert indexed_skills(view) == []
    assert index_drift({"alpha"}, view).identifiers == set()


def test_recall_candidates_leave_out_preloaded_and_flagged_skills(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", "alpha", "Alpha desc")
    write_skill(tmp_path, "beta", "beta", "Beta desc")
    write_skill(tmp_path / "grp", "gamma", "gamma", "Gamma desc")

    candidates = recall_candidates(skill_view(tmp_path, expand=["beta"]))

    assert candidates == [
        RecallCandidate("alpha", "Alpha desc", "alpha"),
        RecallCandidate("grp:gamma", "Gamma desc", "grp.gamma"),
    ]


def test_recall_candidates_follow_the_inject_list(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", "alpha", "Alpha desc")
    write_skill(tmp_path, "beta", "beta", "Beta desc")

    candidates = recall_candidates(skill_view(tmp_path, inject=["beta"]))

    assert [c.identifier for c in candidates] == ["beta"]


def test_drift_note_lists_new_skills_in_the_index_line_shape(tmp_path: Path) -> None:
    write_skill(tmp_path, "beta", "beta", "Beta desc")
    added = indexed_skills(skill_view(tmp_path))

    text = new_skills_note_text(added)

    assert text.startswith("Additional skills are now available beyond those listed")
    assert text.endswith(index_entry("beta", "Beta desc"))


def test_a_bulk_install_is_capped_with_a_counted_tail(tmp_path: Path) -> None:
    extra = 5
    for i in range(NEW_SKILLS_MAX_ENTRIES + extra):
        write_skill(tmp_path, f"skill-{i:02d}", f"skill-{i:02d}", f"Desc {i}")
    added = indexed_skills(skill_view(tmp_path))

    lines = [ln for ln in new_skills_note_text(added).splitlines() if ln.startswith("- ")]

    assert len(lines) == NEW_SKILLS_MAX_ENTRIES + 1
    assert lines[-1].startswith(f"- … and {extra} more")
    assert "meiva.help(meiva.skills)" in lines[-1]
    assert len(skill_identifiers(added)) == NEW_SKILLS_MAX_ENTRIES + extra
