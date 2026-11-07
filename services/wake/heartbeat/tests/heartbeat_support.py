from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog

THRESHOLD_S = 300.0

# meiva:note
# The configured ceiling on the backoff-stretched reminder floor. Selection takes it as a
# value, so a test hands in the same ceiling the daemon passes from its dispatch config.
BACKOFF_MAX_INTERVAL_S = settings.daemon.heartbeat_backoff_max_interval_seconds


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def make_idle(
    db: psycopg.Connection,
    *,
    status_changed_s_ago: float,
    last_active_s_ago: float | None = None,
    paused_until_s_ahead: float | None = None,
    status: str = "idling",
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentityReader,
) -> int:
    if last_active_s_ago is None:
        last_active_s_ago = status_changed_s_ago
    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s WHERE id = %s",
            (status, aid),
        )
        cur.execute(
            "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s), "
            "       last_active_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (status_changed_s_ago, last_active_s_ago, aid),
        )
        if paused_until_s_ahead is not None:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_paused_until = now() + make_interval(secs => %s) "
                "WHERE id = %s",
                (paused_until_s_ahead, aid),
            )
    db.commit()
    return aid
