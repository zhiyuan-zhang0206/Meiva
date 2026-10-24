# meiva:note
# The daemon's selection predicate: which idle agents one heartbeat round checks in on.
# It reads cluster state and returns rows, so a caller can inspect the raw predicate without
# sending anything; the check-in itself stays in `daemon.py`.
# meiva:link services/wake/heartbeat/docs/dispatch-round.meiva.okf.md

from __future__ import annotations

from psycopg_pool import ConnectionPool

from base.deploy.progress_timeout import CORPSE_REAP_GRACE_S
from services.wake.heartbeat import STALE_PENDING_S


def select_idle_agents_needing_heartbeat(
    pool: ConnectionPool,
    idle_threshold_s: float,
    *,
    now: float,
    heartbeat_interval_s: float | None = None,
    jitter_span_s: float = 0.0,
    limit: int | None = None,
    backoff_until: dict[int, float] | None = None,
    corpse_grace_s: float = CORPSE_REAP_GRACE_S,
) -> list[tuple[int, float, str | None]]:
    # meiva:note
    # Cluster-wide idle agents due for a heartbeat check-in, each as
    #     `(agent_id, idle_minutes, reason)`, oldest-idle first; `reason` is
    #     `unapplied_automatic_handoff` when the #5468 narrow exception selected the row
    #     and `None` for the ordinary cadence. That exception is the third rung of the
    #     held-path settle ladder (#5468): a wake retries the owed checkpoint inside its
    #     own budget, a failure stamps the crash marker and idles the row, and this
    #     scanner is what pushes the row once the crash marker exists. It costs a real
    #     model turn, which is why it is narrow, counted and logged apart.
    #
    #     The idle clock is `last_active_at` — the timestamp of the agent's last
    #     completed LLM turn (real work), NOT `status_changed_at`. status_changed_at is
    #     bumped by every status flip including ops lifecycle churn (rollout quiesce /
    #     respawn / update cycles an agent through idling and back),
    #     so keying idle time off it let an ops restart reset the whole fleet's idle
    #     timers. last_active_at is written only by a real turn and is untouched by that
    #     cycle (an idle agent runs no LLM turn through it), so an ops event never resets
    #     an agent's idle timer.
    #
    #     Native agents must be idling; active impersonators are eligible while either
    #     idling or running. During impersonation the clock runs from lease activation,
    #     independent of native turns. The next check-in respects the pause window,
    #     threshold plus jitter, and the durable last-heartbeat interval. Native no-op
    #     backoff does not apply to an external executor whose progress is not observed
    #     through native LLM turns. Preparation and unapplied terminal handoffs stay
    #     excluded until a consumer can receive the heartbeat.
    #
    #     `jitter_span_s` de-phases the idle-clock term by a deterministic per-agent
    #     offset `id mod jitter_span_s` seconds, spreading a fleet that went idle
    #     together across a `jitter_span_s`-wide window so it does not come due (and
    #     wake) in one batch. Deterministic on `id`, so it survives across cycles and
    #     breaks the self-synchronization the check-in itself would otherwise induce. `0`
    #     (the default) disables jitter — the `NULLIF` guards the `mod` against a
    #     divide-by-zero and collapses the offset to 0. Jitter affects only the
    #     idle-clock term; while the pause floor dominates, there is no jitter. `limit`
    #     caps the batch (the hard per-step wake-rate ceiling); with oldest-idle-first
    #     ordering the most overdue agents drain first. Both default to the
    #     un-jittered, unlimited behaviour so existing timing tests read the raw
    #     predicate.
    #
    #     No machine filter: the inbound-insert trigger wakes the agent wherever it
    #     runs.
    #
    #     An idle agent has no running turn task and needs no live lease to receive
    #     a check-in. The host dispatcher starts its next turn from the durable wake.
    #

    # meiva:note
    # Direct callers that inspect the raw idle predicate retain its historic
    # threshold cadence; the daemon supplies its configured check-in interval.
    reminder_interval_s = idle_threshold_s if heartbeat_interval_s is None else heartbeat_interval_s

    # meiva:note
    # EXTRACT returns numeric (Decimal); keep the observed clock compatible
    # with the float slack used when the next dispatch reconciles this reading.
    #
    # Two eligibilities, one cadence. The first is the historic one. The second is the
    # #5468 narrow exception: a row whose held-path settle failed is idling, crash-marked
    # and still owes a handoff receipt — nobody else will ever push it, so a check-in is
    # the recovery. It is scoped to the reap grace (after that the reaper owns the row),
    # keeps every other exclusion, and a genuine silent agent matches neither the marker
    # nor the lease. The owed lease is identified by `status <> 'active' AND
    # handoff_applied_at IS NULL`, not by `automatic`: that column is legacy history since
    # the automatic-only upgrade (a trigger rejects `false` outright), so filtering on it
    # would suggest a mode switch that no longer exists.
    sql = (
        "SELECT id, idle_minutes, "
        " CASE WHEN handoff_recheck THEN 'unapplied_automatic_handoff' END AS reason "
        "FROM ("
        " SELECT agents_meta.id AS id, "
        "  (EXTRACT(EPOCH FROM (now() - "
        "  COALESCE(active.activated_at, active.created_at, last_active_at))) / 60.0"
        "  )::double precision AS idle_minutes, "
        "  COALESCE(active.activated_at, active.created_at, last_active_at) AS last_work_at, "
        "  (agents_meta.status = 'idling' "
        "   AND agents_meta.last_turn_fatal_at IS NOT NULL "
        "   AND agents_meta.last_turn_fatal_at > now() - make_interval(secs => %s) "
        "   AND EXISTS (SELECT 1 FROM agent_impersonations owed "
        "               WHERE owed.agent_id = agents_meta.id "
        "                 AND owed.status <> 'active' "
        "                 AND owed.handoff_applied_at IS NULL) "
        "   AND NOT EXISTS (SELECT 1 FROM agent_impersonations other "
        "                   WHERE other.agent_id = agents_meta.id "
        "                     AND (other.status IN ('requested','accepted') "
        "                          OR (other.status='active' AND other.expires_at <= now()) "
        "                          OR (other.status <> 'active' "
        "                              AND other.delta_version > other.applied_version)))"
        "  ) AS handoff_recheck, "
        "  ((agents_meta.status = 'idling' "
        "    OR (agents_meta.status = 'running' AND active.id IS NOT NULL)) "
        "   AND NOT EXISTS ("
        "    SELECT 1 FROM agent_impersonations lease "
        "    WHERE lease.agent_id = agents_meta.id "
        "      AND (lease.status IN ('requested','accepted') "
        "           OR (lease.status='active' AND lease.expires_at <= now()) "
        "           OR (lease.status <> 'active' AND (lease.delta_version > lease.applied_version "
        "               OR lease.handoff_applied_at IS NULL))) "
        "  )) AS normal_eligible "
        " FROM agents_meta LEFT JOIN agent_impersonations active "
        " ON active.agent_id = agents_meta.id AND active.status = 'active' "
        " AND active.expires_at > now() "
        " WHERE agents_meta.permanent_reject_streak = 0 "
        " AND now() >= GREATEST("
        "   heartbeat_paused_until, "
        "   COALESCE(active.activated_at, active.created_at, last_active_at) "
        "   + make_interval(secs => %s + COALESCE(mod(agents_meta.id, NULLIF(%s, 0)::int), 0)), "
        "   last_heartbeat_at "
        "   + make_interval(secs => LEAST(%s * power(2.0, "
        " CASE WHEN active.id IS NOT NULL THEN 0 ELSE heartbeat_backoff_level END), 86400))"
        " ) "
        " AND NOT EXISTS ("
        "   SELECT 1 FROM inbound_messages im "
        "   WHERE im.agent_id = agents_meta.id AND im.status = 'pending' "
        "     AND im.created_at >= now() - make_interval(secs => %s) "
        " )"
        ") AS scored "
        "WHERE handoff_recheck OR normal_eligible "
        "ORDER BY last_work_at ASC"
    )
    params: list[object] = [
        corpse_grace_s,
        idle_threshold_s,
        jitter_span_s,
        reminder_interval_s,
        STALE_PENDING_S,
    ]
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    # meiva:note
    # Consecutive-failure backoff: skip agents whose absolute backoff deadline
    # has not arrived — a wedged agent must not be poked on the normal cadence.
    # The deadline dict is empty in the common case, so the
    # filter is a fast no-op. The limit is applied AFTER the backoff filter so
    # backed-off agents never consume the per-step wake-rate slots.
    if backoff_until:
        rows = [r for r in rows if now >= backoff_until.get(r[0], 0.0)]
    if limit is not None:
        rows = rows[:limit]
    return rows
