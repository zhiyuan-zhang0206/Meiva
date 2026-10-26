from __future__ import annotations

import psycopg

from base.config import settings


# meiva:note
# Verify that the baseline schema defines deployment_state.min_code_version as a non-null bigint defaulting to 0.
def test_the_migration_restores_the_column_where_the_baseline_has_it(db_conn: None) -> None:
    column = (
        "SELECT data_type, is_nullable, column_default, "
        "col_description('deployment_state'::regclass, ordinal_position::int) "
        "FROM information_schema.columns "
        "WHERE table_name = 'deployment_state' AND column_name = 'min_code_version'"
    )
    with psycopg.connect(settings.data_plane.db_url) as conn:
        baseline = conn.execute(column).fetchone()
        assert baseline is not None and baseline[:3] == ("bigint", "NO", "0")
