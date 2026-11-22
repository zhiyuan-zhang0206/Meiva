"""Consume abandoned inbound rows; no automatic resurrection is performed."""

from psycopg import sql as pgsql
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound import not_passive_inbound
from base.db.transaction import write_transaction


def dead_letter_stale_pending_chats(pool: ConnectionPool, threshold_s: float) -> int:
    """Consume stale pending chats whose terminated owner never claimed them.

    Mark them done without deleting the stored input. Live owners keep the normal
    pending-delivery path. This sweep does not restart terminated owners.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
            "FROM agents_meta am "
            "WHERE m.agent_id = am.id "
            "  AND am.status = 'terminated' "
            "  AND m.status = 'pending' AND m.kind = 'chat' "
            "  AND m.created_at < now() - make_interval(secs => %s)",
            (threshold_s,),
        )
        return cur.rowcount


def dead_letter_stale_claimed(
    pool: ConnectionPool,
    threshold_s: float,
    idling_threshold_s: float = 7200.0,
) -> int:
    """Consume stale claims left by terminated or idling owners.

    Interrupted claims are never returned to pending. Running owners are untouched;
    idling owners use their separate age threshold.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done' "
            "FROM agents_meta am "
            "WHERE m.agent_id = am.id "
            "  AND m.status = 'claimed' AND m.kind IN ('chat', 'system_note', 'heartbeat') "
            "  AND ((am.status = 'terminated' "
            "        AND m.claimed_at < now() - make_interval(secs => %s)) "
            "       OR (am.status = 'idling' "
            "           AND m.claimed_at < now() - make_interval(secs => %s)))",
            (threshold_s, idling_threshold_s),
        )
        return cur.rowcount


def dead_letter_stale_pending_resurrects(pool: ConnectionPool, threshold_s: float) -> int:
    """Dead-letter pending resurrect rows whose consumer never reached claim.

    A stale lifecycle row records an abandoned wake. Retaining it cannot
    recover the turn and later floods the agent with redundant markers, so age
    alone decides cleanup regardless of the current agent lifecycle state.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
            "WHERE m.status = 'pending' AND m.kind = 'resurrect' "
            "  AND m.created_at < now() - make_interval(secs => %s)",
            (threshold_s,),
        )
        return cur.rowcount


def dead_letter_stale_pending_terminated(pool: ConnectionPool, threshold_s: float) -> int:
    """Consume stale lifecycle notices while preserving explicitly passive notes."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            pgsql.SQL(
                "UPDATE inbound_messages m SET status = 'done', claimed_at = now() "
                "FROM agents_meta am "
                "WHERE m.agent_id = am.id "
                "  AND am.status = 'terminated' "
                "  AND m.status = 'pending' "
                "  AND m.kind IN ('terminate', 'system_note') "
                "  AND {not_passive} "
                "  AND m.created_at < now() - make_interval(secs => %s)"
            ).format(not_passive=pgsql.SQL(not_passive_inbound("m"))),
            (threshold_s,),
        )
        return cur.rowcount
