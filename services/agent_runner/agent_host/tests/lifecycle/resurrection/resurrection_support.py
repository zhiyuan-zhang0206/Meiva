import psycopg
from psycopg_pool import ConnectionPool

from base.agents.birth.spawn import create_agent_row
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import PG_KEEPALIVE_KWARGS, Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.lifecycle import termination


def terminated(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    aid, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_identity.name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    db.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
        (aid,),
    )
    db.commit()
    return aid


def status(db: psycopg.Connection, aid: int) -> str:
    row = db.execute("SELECT status FROM agents_meta WHERE id=%s", (aid,)).fetchone()
    db.commit()
    assert row is not None
    return row[0]


def force(aid: int) -> int:
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        _, _, terminate_id, _cutoff = termination.force_terminate_transaction(
            aid, pool, source="user"
        )
    return terminate_id
