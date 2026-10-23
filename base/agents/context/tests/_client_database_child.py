from __future__ import annotations

import sys
from pathlib import Path

from base.agents.context.clients import ClientSet
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL


def main() -> None:
    gate = ProcessDbGate(version=lambda: 2**63 - 1, process="context-database-test")
    database = Database(
        DbConfig(
            db_url=Path(sys.argv[1]).read_text(),
            db_sslmode="disable",
            db_pool_min_size=1,
            db_pool_max_size=2,
            pgbouncer_enabled=False,
        ),
        gate=gate,
    )
    assert settings.data_plane.db_url == PLACEHOLDER_DB_URL
    clients = ClientSet(database=lambda: database)
    try:
        assert clients.sql.execute("SELECT 42").fetchone() == (42,)
    finally:
        clients.close()


if __name__ == "__main__":
    main()
