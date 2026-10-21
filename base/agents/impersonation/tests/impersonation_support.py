from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from base.native_process import native_boot_id
from base.native_process.runtime_incarnation import RuntimeIncarnation


def legacy_session(
    conn: psycopg.Connection, owner: RuntimeIncarnation, *, local_machine: str
) -> dict[str, Any]:
    # meiva:note
    # Seed the historical protocol-less record, without a production mode switch.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,"
            "ttl_seconds,expires_at,accepted_generation,accepted_owner,name,executor_name,"
            "relay_provider,relay_thread_id) "
            "VALUES(%s,%s,'external_agent:codex',%s,'active',300,"
            "clock_timestamp()+interval '5 minutes',%s,%s,'Legacy session','Codex',"
            "'codex','legacy-thread') RETURNING *",
            (uuid4(), owner.agent_id, local_machine, owner.generation, owner.owner),
        )
        row = cur.fetchone()
        assert row is not None
    conn.commit()
    return {**row, "id": str(row["id"])}


def native_identity(birth: float) -> dict[str, object]:
    import sys

    return {
        "created_at": birth,
        "starttime": int(birth * 100) if sys.platform == "linux" else None,
        "boot_id": native_boot_id(),
    }


def recorded_tree() -> dict[str, Any]:
    return {
        "pid": 4242,
        "name": "python3.12",
        "executable": "/usr/bin/python3.12",
        **native_identity(1000.0),
        "parent_pid": 4241,
        "ancestors": [
            {
                "pid": 4241,
                "name": "zsh",
                "executable": "/bin/zsh",
                **native_identity(999.0),
                "parent_pid": 4240,
            },
            {
                "pid": 4240,
                "name": "codex",
                "executable": "/opt/codex",
                **native_identity(998.0),
                "parent_pid": 1,
            },
        ],
    }
