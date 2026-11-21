import psycopg
import pytest
from psycopg.types.json import Jsonb

from gateway.agents.to_user.document import query_agent_cwd


@pytest.mark.parametrize(
    ("channels", "expected"),
    [
        ({"meiva_code__cwd": "/current", "cwd": "/legacy"}, "/current"),
        ({"cwd": "/legacy"}, "/legacy"),
        ({}, None),
    ],
)
def test_file_cwd_reads_latest_root_checkpoint(
    db_conn: psycopg.Connection, channels: dict[str, str], expected: str | None
) -> None:
    rows = [
        ("71", "", "001", Jsonb({"channel_values": {"meiva_code__cwd": "/old"}})),
        ("71", "", "002", Jsonb({"channel_values": channels})),
        ("71", "nested:child", "003", Jsonb({"channel_values": {"cwd": "/child"}})),
        ("72", "", "004", Jsonb({"channel_values": {"cwd": "/other-agent"}})),
    ]
    for row in rows:
        db_conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
            "VALUES (%s, %s, %s, %s, '{}'::jsonb)",
            row,
        )

    assert query_agent_cwd(db_conn, 71) == expected


def test_file_cwd_without_root_checkpoint_is_unset(db_conn: psycopg.Connection) -> None:
    db_conn.execute(
        "INSERT INTO checkpoints "
        "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
        "VALUES ('71', 'nested:child', '001', %s, '{}'::jsonb)",
        (Jsonb({"channel_values": {"cwd": "/child"}}),),
    )

    assert query_agent_cwd(db_conn, 71) is None
    assert query_agent_cwd(db_conn, 72) is None
