from __future__ import annotations

from pathlib import Path

import pytest

from base.lm.claude_sdk import ClusterFact, EntryPoint, Handoff

DEFAULT_CLUSTER = (ClusterFact("macmini", "primary host"),)
DEFAULT_ENTRIES = (EntryPoint("meiva.files", "read and write files"),)


def handoff(
    tmp_path: Path,
    *,
    agent_id: int = 6984,
    instruction: str = "Read hello.txt and reply with the third word.",
    cluster: tuple[ClusterFact, ...] = DEFAULT_CLUSTER,
    entries: tuple[EntryPoint, ...] = DEFAULT_ENTRIES,
) -> Handoff:
    return Handoff(
        agent_id=agent_id,
        instruction=instruction,
        machine="macmini",
        workspace=tmp_path,
        label="claude-sdk-bridge",
        cluster=cluster,
        entries=entries,
    )


def test_render_carries_the_six_sections_in_order(tmp_path: Path) -> None:
    text = handoff(tmp_path).render()
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## 1. Identity",
        "## 2. Cluster",
        "## 3. Entry points",
        "## 4. Do not use",
        "## 5. Reporting",
        "## 6. Task",
    ]


def test_render_mentions_identity_and_task(tmp_path: Path) -> None:
    text = handoff(tmp_path).render()
    assert "Meiva agent #6984 (claude-sdk-bridge)" in text
    assert f"- workspace: {tmp_path}" in text
    assert "- machine: macmini" in text
    assert text.rstrip().endswith("Read hello.txt and reply with the third word.")


def test_size_bytes_matches_the_encoded_render(tmp_path: Path) -> None:
    subject = handoff(tmp_path)
    assert subject.size_bytes() == len(subject.render().encode("utf-8"))


def test_empty_instruction_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="instruction is empty"):
        handoff(tmp_path, instruction="   ")


def test_non_positive_agent_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="agent id must be positive"):
        handoff(tmp_path, agent_id=0)


def test_defaults_cover_the_overlap_and_reporting_rules(tmp_path: Path) -> None:
    text = handoff(tmp_path).render()
    assert "background-task or monitor machinery" in text
    assert "One reply per turn" in text


def test_empty_cluster_and_entries_render_placeholders(tmp_path: Path) -> None:
    text = handoff(tmp_path, cluster=(), entries=()).render()
    assert text.count("- (none)") == 2
