from __future__ import annotations

import argparse
import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from base.sessions import provenance
from meiva import agents
from meiva.shell.coding_tools import _common, add_lineage_arguments, require_lineage

NOW = dt.datetime(2026, 10, 18, 15, 41, tzinfo=dt.UTC)


def _prov(**overrides: object) -> provenance.DispatchProvenance:
    fields: dict[str, object] = {
        "parent_agent_id": 41,
        "parent_label": "worker",
        "spawn_chain": ("7", "41"),
        "dispatched_at": NOW,
        "why": "the reviewer's context is full",
        "task_id": 9,
        "task_label": "lineage",
        "priority": "P1",
    }
    fields.update(overrides)
    return provenance.DispatchProvenance(**cast("dict[str, Any]", fields))


def _ancestors(*ids: int) -> list[agents.Neighbor]:
    return [
        agents.Neighbor(
            agent_id=agent_id,
            label=None,
            status=agents.AgentStatus.RUNNING,
            depth=1,
            score=1.0,
        )
        for agent_id in ids
    ]


def _row(agent_id: int, label: str | None) -> agents.AgentRow:
    return agents.AgentRow(
        agent_id=agent_id,
        label=label,
        status=agents.AgentStatus.RUNNING,
        spawner="user",
        fork_source_agent_id=None,
        machine="ubuntu",
        spawned_at=NOW,
        started_at=None,
        last_active_at=NOW,
        last_inbound_at=NOW,
        pid=None,
        heartbeat_paused_until=None,
    )


def _directory(*rows: agents.AgentRow) -> Callable[..., agents.AgentDirectoryPage]:
    page = agents.AgentDirectoryPage(agents=list(rows), next_cursor=None)

    def _read(*_args: object, **_kwargs: object) -> agents.AgentDirectoryPage:
        return page

    return _read


def test_build_collects_the_parent_chain_and_label() -> None:
    prov = _common.build_dispatch_provenance(
        for_task=9,
        why="because",
        task_label="lineage",
        priority="P1",
        now=NOW,
        agent_id=lambda: 41,
        ancestors=lambda _agent_id: _ancestors(7, 1),
        directory=_directory(_row(41, "worker")),
    )

    assert prov.spawn_chain == ("1", "7", "41")
    assert prov.parent_label == "worker"
    assert prov.task_id == 9


def test_build_degrades_instead_of_failing() -> None:
    def _explode(_agent_id: int) -> list[agents.Neighbor]:
        raise RuntimeError("gateway down")

    prov = _common.build_dispatch_provenance(
        for_task=None,
        why="because",
        agent_id=lambda: 41,
        ancestors=_explode,
        directory=_directory(),
    )
    text = provenance.render(prov)

    assert prov.spawn_chain == ()
    assert "dispatched_by: agent 41" in text
    assert "for_task: none (ad-hoc)" in text


def test_bootstrap_puts_the_block_first(tmp_path: Path) -> None:
    block = provenance.render(_prov())

    message = _common.worker_bootstrap(
        tmp_path / "contract.md",
        tmp_path,
        tmp_path / "tasks.md",
        tmp_path / "work.md",
        provenance=block,
    )

    assert message.startswith(f"{block}\n\n")
    assert "Read the collaboration contract at" in message


def test_task_file_header_is_written_once(tmp_path: Path) -> None:
    tasks_file = tmp_path / "tasks.md"
    block = provenance.render(_prov())

    _common.write_task_file_header(tasks_file, block)
    first = tasks_file.read_text(encoding="utf-8")
    _common.write_task_file_header(tasks_file, block)

    assert tasks_file.read_text(encoding="utf-8") == first
    assert first.count(provenance.MARKER) == 1


def test_task_file_header_keeps_the_operator_text_below_it(tmp_path: Path) -> None:
    tasks_file = tmp_path / "tasks.md"
    tasks_file.write_text("fix the bug\n", encoding="utf-8")
    block = provenance.render(_prov())

    _common.write_task_file_header(tasks_file, block)

    text = tasks_file.read_text(encoding="utf-8")
    assert text.startswith(block)
    assert text.endswith("fix the bug\n")


def test_task_file_header_leaves_an_existing_block_alone(tmp_path: Path) -> None:
    tasks_file = tmp_path / "tasks.md"
    original = f"{provenance.MARKER}\nold block\n"
    tasks_file.write_text(original, encoding="utf-8")

    _common.write_task_file_header(tasks_file, provenance.render(_prov()))

    assert tasks_file.read_text(encoding="utf-8") == original


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spawn-test")
    add_lineage_arguments(parser)
    return parser


def test_a_missing_reason_is_a_usage_error() -> None:
    parser = _parser()
    args = parser.parse_args(["--for-task", "9"])

    with pytest.raises(SystemExit):
        require_lineage(parser, args)


def test_task_metadata_without_a_task_is_a_usage_error() -> None:
    parser = _parser()
    args = parser.parse_args(["--why", "because", "--priority", "P1"])

    with pytest.raises(SystemExit):
        require_lineage(parser, args)


def test_the_block_is_generated_from_validated_flags() -> None:
    parser = _parser()
    args = parser.parse_args(
        ["--why", "because", "--for-task", "9", "--task-label", "lineage", "--priority", "P1"]
    )
    seen: dict[str, object] = {}

    def _build(**kwargs: object) -> provenance.DispatchProvenance:
        seen.update(kwargs)
        return _prov()

    text = require_lineage(parser, args, build=_build)

    assert seen == {
        "for_task": 9,
        "why": "because",
        "task_label": "lineage",
        "priority": "P1",
    }
    assert text.startswith(provenance.MARKER)


def test_a_takeover_gets_the_three_line_form() -> None:
    parser = _parser()
    args = parser.parse_args(["--why", "because"])

    def _build(**_kwargs: object) -> provenance.DispatchProvenance:
        return _prov()

    text = require_lineage(parser, args, takeover=True, build=_build)

    assert text.splitlines() == [
        'requested_by: agent 41 "worker"',
        "why: the reviewer's context is full",
        "return_to: agent 41",
    ]
    assert len(text.splitlines()) == 3


def test_a_long_label_cannot_push_the_tail_out_of_the_block() -> None:
    text = provenance.render(_prov(parent_label="L" * 600, task_label="T" * 600, why="w " * 3000))

    assert len(text) <= provenance.MAX_BLOCK_CHARS
    assert "report_to: agent 41" in text
    assert "must_report: for_task id" in text
    assert text.endswith("conclusion")


def test_a_label_is_bounded_but_still_named() -> None:
    long_label = provenance.render(_prov(parent_label="L" * 200))

    assert "L" * provenance.MAX_LABEL_CHARS in long_label
    assert "L" * (provenance.MAX_LABEL_CHARS + 1) not in long_label
    assert "report_to: agent 41" in long_label


def test_block_names_dispatcher_task_and_return_address() -> None:
    text = provenance.render(_prov())

    assert text.splitlines() == [
        "## Who dispatched you (generated - quote it when you report; do not edit)",
        'dispatched_by: agent 41 "worker" (spawn_chain: 7 > 41)',
        'for_task: 9 "lineage" (P1)',
        "dispatched_at: 2026-10-18T15:41+00:00",
        "why_you: the reviewer's context is full",
        "report_to: agent 41",
        "must_report: for_task id + absolute path of every deliverable + a one-line conclusion",
    ]


def test_without_a_task_the_block_says_so() -> None:
    text = provenance.render(_prov(task_id=None, task_label=None, priority=None))

    assert "for_task: none (ad-hoc)" in text


def test_without_identity_the_block_stays_honest() -> None:
    text = provenance.render(_prov(parent_agent_id=None, parent_label=None, spawn_chain=()))

    assert "dispatched_by: unknown (no agent identity in the launching process)" in text
    assert "report_to: unknown" in text


def test_quotes_in_labels_cannot_break_the_fields() -> None:
    text = provenance.render(_prov(parent_label='Bad "label"', task_label='a "b"'))

    assert "dispatched_by: agent 41 \"Bad 'label'\"" in text
    assert "for_task: 9 \"a 'b'\"" in text


def test_every_block_fits_the_budget() -> None:
    text = provenance.render(
        _prov(
            why="x" * 4000,
            task_label="y" * 2000,
            parent_label="z" * 500,
            spawn_chain=("1", "2", "3", "4", "5"),
        )
    )

    assert len(text) <= provenance.MAX_BLOCK_CHARS
    assert text.startswith(provenance.MARKER)
    assert "report_to: agent 41" in text


def test_a_long_reason_is_shortened_with_a_visible_mark() -> None:
    text = provenance.render(_prov(why="word " * 400, task_label=None))

    assert len(text) <= provenance.MAX_BLOCK_CHARS
    assert "\u2026" in text
    assert "report_to: agent 41" in text


def test_marker_detects_an_existing_block() -> None:
    assert provenance.has_marker(provenance.render(_prov()))
    assert not provenance.has_marker("tasks only")
