# meiva:note
# Queues a durable command (cancel, terminate, restart, chat) for an agent the way a gateway
# route or a peer would, so a test that needs an interrupt writes the row the watcher reads
# instead of replacing the watcher. `waited_s` backdates the row: a restart interrupts only after
# its grace has passed on the database clock.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md

import psycopg


def queue_command(
    conn: psycopg.Connection,
    agent_id: int,
    kind: str,
    *,
    source: str = "user",
    waited_s: float = 0.0,
) -> None:
    conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, created_at) "
        "VALUES (%s, '', %s, %s, clock_timestamp() - make_interval(secs => %s))",
        (agent_id, kind, source, waited_s),
    )
    conn.commit()
