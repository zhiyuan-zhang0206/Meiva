# meiva:note
# The upstream checkpoint schema dependency used by first-start provisioning.
# meiva:link base/cluster/docs/postgres.meiva.okf.md

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class CheckpointSchema(Protocol):
    @property
    def migrations(self) -> Sequence[str]: ...

    def setup(
        self, *, base_admin_url: str, identity: str, expected_data_dir: Path | None
    ) -> None: ...


class PostgresCheckpointSchema:
    @property
    def migrations(self) -> Sequence[str]:
        from langgraph.checkpoint.postgres import PostgresSaver

        return PostgresSaver.MIGRATIONS

    def setup(self, *, base_admin_url: str, identity: str, expected_data_dir: Path | None) -> None:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        from base.cluster.dataplane.pg_admin import owner_session

        with owner_session(
            base_admin_url,
            database=identity,
            owner=identity,
            expected_data_dir=expected_data_dir,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        ) as conn:
            PostgresSaver(conn).setup()
