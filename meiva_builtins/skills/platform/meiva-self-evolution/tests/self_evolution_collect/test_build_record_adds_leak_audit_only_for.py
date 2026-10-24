"""Self evolution collect cases: build record adds leak audit only for."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.db import Database
from meiva_builtins.skills.tests.skill_script import load_skill_script

world = load_skill_script("platform", "meiva-self-evolution", "tests", "collection_world.py")
record_mod = load_skill_script("platform", "meiva-self-evolution", "scripts", "record.py")
window_mod = load_skill_script("platform", "meiva-self-evolution", "scripts", "events_window.py")


def test_build_record_adds_leak_audit_only_for_eval_collection(tmp_path: Path) -> None:
    """Weekly collection remains unchanged; eval collection records invalidating reads."""
    paths = record_mod.LeakPaths(
        memory_pool=str(tmp_path / "memory"),
        self_evolution=str(tmp_path / "self_evolution"),
        workspaces_root=str(tmp_path / "workspaces"),
        agent_workspace=str(tmp_path / "workspaces" / "1"),
    )
    values: dict[str, Any] = {
        "agent_id": 1,
        "week": "2026-W99",
        "events": (("code", {"body": f"open('{paths.self_evolution}/reports/result.md')"}),),
        "log_events": (),
        "inbounds": (),
        "meta": record_mod.AgentMeta("user", "completed", "done"),
        "transcript": (),
        "skills_touched": [],
    }
    weekly = record_mod.build_record(record_mod.RunInputs(**values))
    audited = record_mod.build_record(record_mod.RunInputs(**values, leak_paths=paths))

    assert "leak_audit" not in weekly
    assert "invalidated" not in weekly
    assert audited["invalidated"] is True
    assert audited["leak_audit"][0]["surface"] == "results"


def test_test_label_ids_matches_prefix_only(collect_mod: Any, db_conn: psycopg.Connection) -> None:
    """Only TEST- prefixed role labels are test spawns: a plain agent label
    and a NULL label must stay in the dataset."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id, label) VALUES (9001, 'TEST-bench-on-t1'), "
            "(9002, 'test-agent-9002'), (9003, NULL)"
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        ids = collect_mod._test_label_ids(cur, [9001, 9002, 9003, 99999])

    assert ids == {9001}


def test_test_label_ids_excludes_direct_children_of_test_orchestrators(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id, label) VALUES (9010, 'TEST-orchestrator'), "
            "(9011, 'ordinary-child'), (9012, 'ordinary-agent')"
        )
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES "
            "(9010, 'user', 'terminated'), (9011, 'agent:9010', 'terminated'), "
            "(9012, 'user', 'terminated')"
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        ids = collect_mod._test_label_ids(cur, [9010, 9011, 9012])

    assert ids == {9010, 9011}


# meiva:note
# collect_with_counts() returns the records plus the pre-record counts the daily sentinel keys on: distinct window agents seen, TEST- drops, missing-lifecycle drops (QA review of PR #698).
def test_collect_with_counts_keeps_records_and_reports_filters(
    collect_mod: Any, db_conn: psycopg.Connection, database: Database
) -> None:
    rows = [world.event_row(i, world.ts_after(i), agent_id=i) for i in (1, 2, 3, 4)]
    with db_conn.cursor() as cur:
        world.insert_agent(cur, 1)
        world.insert_agent(cur, 2, label="TEST-bench-on-t1")
        world.insert_agent(cur, 3)
    db_conn.commit()
    window = window_mod.Window(world.ts_after(0), world.ts_after(100))

    with world.serve_events(rows) as server:
        gateway = world.grant(server.url)
        records, counts = collect_mod.collect_with_counts(
            window, "2026-08-26", database=database, gateway=gateway
        )

        assert counts == {"seen": 4, "excluded_test": 1, "skipped_meta": 1}
        assert [r["agent_id"] for r in records] == [1, 3]

        # include_test opts the TEST- spawns back in for measurement runs.
        records_all, counts_all = collect_mod.collect_with_counts(
            window, "2026-08-26", database=database, gateway=gateway, include_test=True
        )

    assert counts_all == {"seen": 4, "excluded_test": 0, "skipped_meta": 1}
    assert [r["agent_id"] for r in records_all] == [1, 2, 3]
    assert {r["week"] for r in records_all} == {"2026-08-26"}


@pytest.fixture(scope="module")
def collect_mod() -> Any:
    """The collection logic module, loaded by path (kebab-case skill dirs never import)."""
    return load_skill_script("platform", "meiva-self-evolution", "scripts", "collect.py")
