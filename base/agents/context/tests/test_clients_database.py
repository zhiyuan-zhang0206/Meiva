from base.agents.context.clients import ClientSet
from base.db.code_version_gate import ProcessDbGate


def test_custom_database_dials_its_own_resource_with_no_process_default(
    db_url: str, database_gate: ProcessDbGate
) -> None:
    from base.config import get_field, set_field
    from base.db import Database

    database = Database.from_settings(gate=database_gate)
    clients = ClientSet(database=lambda: database)
    prior = get_field("db_url")
    set_field("db_url", "")
    try:
        assert clients.sql.execute("SELECT 42").fetchone() == (42,)
    finally:
        set_field("db_url", prior)
        clients.close()
