from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.lm.harness import ClusterFact, EntryPoint, Handoff, HarnessContribution
from base.lm.harness.handoff import HANDOFF_VERSION

DEFAULT_CLUSTER = (ClusterFact("macmini", "primary host"),)
DEFAULT_ENTRIES = (EntryPoint("meiva.tasks", "read and update tasks"),)


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
        "## Meiva & Your Cluster Identity",
        "## Cluster Machines",
        "## Using the Meiva Python SDK",
        "## Tool & Environment Boundaries",
        "## Collaboration & Reporting",
        "## Standing Instructions",
    ]


def test_render_mentions_identity_and_task(tmp_path: Path) -> None:
    text = handoff(tmp_path).render()
    assert "# Role & Operating Context — Agent #6984 (claude-sdk-bridge)" in text
    assert "Handoff" not in text
    assert f"- workspace: {tmp_path}" in text
    assert "- machine: macmini" in text
    assert "python3 -c '" in text
    assert "import meiva" in text
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


def test_defaults_cover_the_boundary_and_reporting_rules(tmp_path: Path) -> None:
    text = Handoff(
        agent_id=6984,
        instruction="Read hello.txt",
        machine="macmini",
        workspace=tmp_path,
    ).render()
    assert "meiva.files" not in text
    assert "meiva.tasks" not in text
    assert "meiva.watcher" not in text
    assert "meiva.agents" in text
    assert "meiva.self" in text
    assert "pause_heartbeat" in text
    assert "terminate" in text
    assert "Use your native tools for files" in text
    assert "State what you did and the evidence for it" in text


def test_with_contributions_filters_by_harness_and_deduplicates(tmp_path: Path) -> None:
    base = Handoff(
        agent_id=6984,
        instruction="Read hello.txt",
        machine="macmini",
        workspace=tmp_path,
    )
    contributions = (
        HarnessContribution(
            harnesses=frozenset({Harness.CODEX, Harness.CLAUDE_CODE}),
            entries=(
                EntryPoint("meiva.tasks", "the task you were given, and its siblings"),
                EntryPoint("meiva.agents", "duplicate should be ignored"),
            ),
            discipline=("Report progress to your agreed delegator.",),
        ),
        HarnessContribution(
            harnesses=frozenset({Harness.ANTIGRAVITY}),
            entries=(EntryPoint("meiva.agy_only", "antigravity only"),),
        ),
    )
    merged = base.with_contributions(contributions, harness=Harness.CODEX)
    text = merged.render()
    assert "meiva.tasks" in text
    assert "duplicate should be ignored" not in text
    assert "meiva.agy_only" not in text
    assert "Report progress to your agreed delegator." in text


def test_empty_cluster_and_entries_render_placeholders(tmp_path: Path) -> None:
    text = handoff(tmp_path, cluster=(), entries=()).render()
    assert text.count("- (none)") == 2


# meiva:note
# The handoff is a wire format, not an internal dataclass: every provider adapter
# renders the same document and a waking agent reads it as its first message. So
# the field set is pinned here against the version — an adapter that reads
# version 1 must never silently receive a different shape. Add a field, bump
# HANDOFF_VERSION, extend this tuple; anything less fails here first.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
PINNED_HANDOFF_FIELDS = (
    "agent_id",
    "instruction",
    "machine",
    "workspace",
    "label",
    "cluster",
    "entries",
    "overlaps",
    "discipline",
)


def test_field_set_is_pinned_to_the_handoff_version() -> None:
    names = tuple(entry.name for entry in fields(Handoff))
    assert HANDOFF_VERSION == 1, (
        "HANDOFF_VERSION moved without PINNED_HANDOFF_FIELDS following it; the "
        "version pins the field set every adapter reads"
    )
    assert names == PINNED_HANDOFF_FIELDS, (
        "Handoff fields changed: bump HANDOFF_VERSION and update "
        "PINNED_HANDOFF_FIELDS in the same commit"
    )
