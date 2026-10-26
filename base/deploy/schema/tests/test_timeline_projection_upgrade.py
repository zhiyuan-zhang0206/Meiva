from __future__ import annotations

import psycopg

_TIMELINE_TABLES = (
    "run_timeline_segments",
    "run_timeline_message_facts",
    "run_timeline_blocks",
)


# meiva:note
# Verify the folded baseline carries the run-timeline projection tables and indexes.
def test_timeline_projection_upgrade_is_discovered_and_preserves_history(
    db_conn: psycopg.Connection,
) -> None:
    for table in _TIMELINE_TABLES:
        assert db_conn.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (table,)
    assert (
        db_conn.execute(
            "SELECT indexname FROM pg_indexes WHERE indexname IN "
            "('run_timeline_blocks_start_idx', 'run_timeline_blocks_end_idx', "
            "'run_timeline_message_start_idx', 'run_timeline_message_end_idx', "
            "'understanding_nodes_span_end_idx')"
        ).rowcount
        == 5
    )
