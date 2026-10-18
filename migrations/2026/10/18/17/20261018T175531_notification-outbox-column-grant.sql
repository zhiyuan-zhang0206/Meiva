-- The delivery log is the outbox: a producer commits the row, the announcer marks
-- `announced_at` once the live event was published, and the gateway sweeps the
-- rows still owing one. The runner announced its own `core.turn_finished`
-- deliveries, so it needs to write that mark -- and nothing else on the row.
--
-- A table-wide UPDATE also lets the role rewrite a delivery's subject or body,
-- which no runner path does and which would silently change what the user is
-- told. The grant is therefore column-scoped, the same shape the runner already
-- holds on `agent_impersonations` (`base/cluster/authority/event_grants.py`).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        REVOKE UPDATE ON notification_events FROM meiva_runner;
        GRANT UPDATE (announced_at) ON notification_events TO meiva_runner;
    END IF;
END $$;
