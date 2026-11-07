#!/usr/bin/env bash
# Apply the baseline db/schema.sql on a fresh empty DB + exercise trigger paths,
# guarding against schema bodies silently referencing wrong column / table names
# (CREATE OR REPLACE FUNCTION does not check column reference validity; a
# stand-alone apply will not blow up).
#
# Since the 2026-07-19 re-baseline, db/schema.sql is the squashed baseline (the
# full current schema, including trigger functions); migrations/ holds only
# post-baseline deltas. So the fresh-bootstrap smoke applies schema.sql, not the
# migration sequence — the trigger bodies it exercises now live there.
#
# Bug this shape catches (2026-05-20, then folded into the baseline): a
# cascade_close_agent_pages() written as NEW.agent_id while the trigger fires on
# agents_meta (column is `id`), causing every UPDATE status='terminated' to 5xx.
#
# Usage:
#   MEIVA_DB_URL=postgresql://meiva@host:5432/ scripts/ci/test_migrations_apply.sh
#   (or directly PGHOST=postgres PGUSER=meiva scripts/ci/test_migrations_apply.sh)
# Default: connect to postgres:5432 (CI container service sidecar default name).
set -euo pipefail

PGHOST="${PGHOST:-${MEIVA_PGHOST:-postgres}}"
PGUSER="${PGUSER:-${MEIVA_PGUSER:-meiva}}"
PGPORT="${PGPORT:-5432}"
ADMIN_DB="${PGDATABASE:-meiva}"
export PGHOST PGUSER PGPORT

TEST_DB="meiva_migration_smoke_$$"
FULL_DB="meiva_migration_full_$$"

cleanup() {
    psql -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $TEST_DB" >/dev/null 2>&1 || true
    psql -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $FULL_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

REPO_ROOT="${MEIVA_REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO_ROOT"

echo "-> create $TEST_DB on $PGHOST:$PGPORT"
psql -d "$ADMIN_DB" -v ON_ERROR_STOP=1 -c "CREATE DATABASE $TEST_DB"

# The group roles are created before the baseline so the role-scoped GRANT blocks
# inside it actually run; without them those blocks are no-ops and an object that
# only the owner can read still passes this smoke (the 2026-10-18 live incident).
echo "-> create the reader roles so role-scoped grants are exercised"
psql -d "$ADMIN_DB" -v ON_ERROR_STOP=1 -q <<'SQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_runner') THEN
        CREATE ROLE meiva_runner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meiva_gateway') THEN
        CREATE ROLE meiva_gateway NOLOGIN;
    END IF;
END $$;
SQL

echo "-> apply baseline db/schema.sql"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 -q -f db/schema.sql
echo "  ok"

# A schema-owner run proves the DDL parses, not that a service can read it.
# The catalog check runs on any connection; the read below additionally needs
# SET ROLE (superuser or membership), so it stays guarded — but never silently:
# the ACL check above it fails on its own when a grant is missing.
echo "-> a service role holds SELECT on the attribution view and its evidence table"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 -q -o /dev/null <<'SQL'
DO $$
BEGIN
    IF NOT (has_table_privilege('meiva_runner', 'public.telemetry_events_attributed', 'SELECT')
            AND has_table_privilege('meiva_runner', 'public.telemetry_owner_inference', 'SELECT')
            AND has_table_privilege('meiva_gateway', 'public.telemetry_events_attributed', 'SELECT')) THEN
        RAISE EXCEPTION 'a service role cannot read the attribution view (empty ACL?)';
    END IF;
END $$;
SQL
echo "  ok"
echo "-> a service role read really returns rows"
if [ "$(psql -d "$TEST_DB" -At -c "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")" = "t" ]; then
    psql -d "$TEST_DB" -v ON_ERROR_STOP=1 -q -o /dev/null <<'SQL'
SET ROLE meiva_runner;
SELECT count(*) FROM telemetry_events_attributed;
SELECT count(*) FROM telemetry_owner_inference;
RESET ROLE;
SET ROLE meiva_gateway;
SELECT count(*) FROM telemetry_events_attributed;
RESET ROLE;
SQL
    echo "  ok"
else
    echo "  skipped: SET ROLE needs a superuser (the ACL check above still ran)"
fi

echo "-> retired deploy storage is absent and the version-gate row survives"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
    IF to_regclass('public.cluster_pin') IS NOT NULL
       OR to_regclass('public.cluster_last_update') IS NOT NULL
       OR to_regclass('public.agent_watchers') IS NOT NULL
       OR to_regprocedure('public.lock_runtime_publication_admission()') IS NOT NULL THEN
        RAISE EXCEPTION 'retired deploy or watcher storage is back in the baseline';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'agents_meta'
                 AND column_name = 'closed_at') THEN
        RAISE EXCEPTION 'agents_meta.closed_at is back in the baseline';
    END IF;
    IF (SELECT array_agg(column_name::text ORDER BY column_name)
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'deployment_state')
       IS DISTINCT FROM ARRAY['id', 'min_code_version'] THEN
        RAISE EXCEPTION 'deployment_state must hold only id and min_code_version';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM deployment_state WHERE id = 1) THEN
        RAISE EXCEPTION 'the deployment_state singleton row is missing (version gate)';
    END IF;
END $$;
SQL
echo "  ok"

echo "-> audit_events is append-only: INSERT lands, UPDATE / DELETE / TRUNCATE are rejected"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source)
    VALUES (1, now(), 'smoke-machine', 'smoke', 'spawn', 'info', 'user');
DO $$
DECLARE
    statement TEXT;
BEGIN
    FOREACH statement IN ARRAY ARRAY[
        'UPDATE audit_events SET level = ''error''',
        'DELETE FROM audit_events',
        'TRUNCATE audit_events'
    ] LOOP
        BEGIN
            EXECUTE statement;
            RAISE EXCEPTION 'audit_events accepted: %', statement;
        EXCEPTION WHEN raise_exception THEN
            IF SQLERRM NOT LIKE 'audit_events is append-only%' THEN
                RAISE;
            END IF;
        END;
    END LOOP;
    IF (SELECT count(*) FROM audit_events) <> 1 THEN
        RAISE EXCEPTION 'audit_events lost its row';
    END IF;
END $$;
SQL
echo "  ok"

echo "-> telemetry_events is append-only and partitioned: INSERT lands in its month, UPDATE / DELETE / TRUNCATE are rejected"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
SELECT ensure_telemetry_event_partitions(3);
INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, event_name, level, source)
    VALUES (1, now(), 'smoke-machine', 'smoke', 'smoke', 'telemetry', 'llm_usage', 'info', 'system');
DO $$
DECLARE
    statement TEXT;
BEGIN
    IF (SELECT tableoid::regclass::text FROM telemetry_events WHERE event_uid = 1) = 'telemetry_events_default' THEN
        RAISE EXCEPTION 'the current month has no partition';
    END IF;
    FOREACH statement IN ARRAY ARRAY[
        'UPDATE telemetry_events SET level = ''error''',
        'DELETE FROM telemetry_events',
        'TRUNCATE telemetry_events'
    ] LOOP
        BEGIN
            EXECUTE statement;
            RAISE EXCEPTION 'telemetry_events accepted: %', statement;
        EXCEPTION WHEN raise_exception THEN
            IF SQLERRM NOT LIKE 'telemetry_events is append-only%' THEN
                RAISE;
            END IF;
        END;
    END LOOP;
    IF (SELECT count(*) FROM telemetry_events) <> 1 THEN
        RAISE EXCEPTION 'telemetry_events lost its row';
    END IF;
END $$;
SQL
echo "  ok"

echo "-> trigger smoke: exercise agents_meta termination triggers"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO agents (label) VALUES ('smoke-agent');
INSERT INTO agents_meta (id, status, machine)
    VALUES ((SELECT max(id) FROM agents), 'running', 'smoke-machine');
UPDATE agents_meta SET status = 'terminated'
    WHERE id = (SELECT max(id) FROM agents);

-- Observed lifecycle time follows real row transitions, even without telemetry.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM agent_lifecycle_intervals
        WHERE agent_id = (SELECT max(id) FROM agents) AND ended_at >= started_at
    ) THEN
        RAISE EXCEPTION 'termination did not close the lifecycle interval';
    END IF;
END $$;

-- Check resurrection only after termination.
UPDATE agents_meta SET status = 'idling'
    WHERE id = (SELECT max(id) FROM agents);
UPDATE agents_meta SET status = 'running'
    WHERE id = (SELECT max(id) FROM agents);
DO $$
BEGIN
    IF (SELECT count(*) FROM agent_lifecycle_intervals
        WHERE agent_id = (SELECT max(id) FROM agents)) <> 2
       OR (SELECT count(*) FROM agent_lifecycle_intervals
           WHERE agent_id = (SELECT max(id) FROM agents) AND ended_at IS NULL) <> 1 THEN
        RAISE EXCEPTION 'resurrection or nonterminal transition corrupted lifecycle intervals';
    END IF;
END $$;

-- Lifecycle status transitions preserve spawn lineage, even when the parent is
-- terminated. This protects against a trigger reintroducing a spawner rewrite.
INSERT INTO agents (id, label) VALUES
    (991001, 'spawner-smoke-grandparent'),
    (991002, 'spawner-smoke-parent'),
    (991003, 'spawner-smoke-child'),
    (991004, 'spawner-smoke-terminated-child');
INSERT INTO agents_meta (id, spawner, status) VALUES
    (991001, 'user', 'running'),
    (991002, 'agent:991001', 'running'),
    (991003, 'agent:991002', 'running'),
    (991004, 'agent:991002', 'terminated');
UPDATE agents_meta SET status = 'terminated' WHERE id = 991002;
UPDATE agents_meta SET status = 'idling' WHERE id = 991004;

DO $$
DECLARE child_spawner TEXT;
DECLARE resurrected_spawner TEXT;
BEGIN
    SELECT spawner INTO child_spawner FROM agents_meta WHERE id = 991003;
    IF child_spawner <> 'agent:991002' THEN
        RAISE EXCEPTION 'terminating parent rewrote child spawner — child_spawner=%',
            child_spawner;
    END IF;
    SELECT spawner INTO resurrected_spawner FROM agents_meta WHERE id = 991004;
    IF resurrected_spawner <> 'agent:991002' THEN
        RAISE EXCEPTION 'resurrecting agent rewrote spawner — resurrected_spawner=%',
            resurrected_spawner;
    END IF;
END $$;
SQL

echo "-> harness switch & external messages smoke: mutable harness + agent_harness_messages insert/select"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
-- `harness` can now be switched on restart. Verify mutable harness and agent_harness_messages round-trip.
INSERT INTO agents (id, label) VALUES (993001, 'harness-smoke');
INSERT INTO agents_meta (id, status, harness) VALUES (993001, 'idling', 'codex');
DO $$
DECLARE stored TEXT;
        msg_count INT;
BEGIN
    IF (SELECT harness FROM agents_meta WHERE id = 993001) <> 'codex' THEN
        RAISE EXCEPTION 'the explicit harness was not stored';
    END IF;
    UPDATE agents_meta SET harness = 'meiva' WHERE id = 993001;
    SELECT harness INTO stored FROM agents_meta WHERE id = 993001;
    IF stored <> 'meiva' THEN
        RAISE EXCEPTION 'harness update did not succeed — stored=%', stored;
    END IF;

    INSERT INTO agent_harness_messages (agent_id, seq, harness, item)
    VALUES (993001, 0, 'codex', '{"kind": "human", "text": "hello"}'::jsonb);

    SELECT count(*) INTO msg_count FROM agent_harness_messages WHERE agent_id = 993001;
    IF msg_count <> 1 THEN
        RAISE EXCEPTION 'agent_harness_messages insert failed — msg_count=%', msg_count;
    END IF;
END $$;
SQL

echo "-> born_spawner baseline smoke: append-only trigger"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE rejected_update BOOLEAN := FALSE;
BEGIN
    BEGIN
        UPDATE agents_meta SET born_spawner = 'agent:1' WHERE id = 991001;
    EXCEPTION WHEN raise_exception THEN rejected_update := TRUE;
    END;
    IF NOT rejected_update THEN
        RAISE EXCEPTION 'born_spawner update was not rejected';
    END IF;
END $$;
SQL

echo "-> convergence: db/schema.sql alone vs baseline-pending migrations (pg_dump --schema-only)"
psql -d "$ADMIN_DB" -v ON_ERROR_STOP=1 -c "CREATE DATABASE $FULL_DB"
psql -d "$FULL_DB" -v ON_ERROR_STOP=1 -q -f db/schema.sql
while IFS= read -r f; do
    migration_name="${f##*/}"
    migration_name="${migration_name%.sql}"
    # A current baseline can fold a non-idempotent migration and stamp its name
    # in schema_migrations. Match the runtime applier: fresh DBs skip that
    # already-represented delta; existing DBs without the marker execute it.
    if psql -d "$FULL_DB" -v ON_ERROR_STOP=1 -v migration_name="$migration_name" -Atq <<'SQL' | grep -qx 't'
SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = :'migration_name');
SQL
    then
        continue
    fi
    psql -d "$FULL_DB" -v ON_ERROR_STOP=1 -q -f "$f"
done < <(find migrations -type f -name '*.sql' | awk -F / '{print $NF "\t" $0}' | LC_ALL=C sort | cut -f 2-)

BASELINE_DUMP=$(mktemp)
FULL_DUMP=$(mktemp)
pg_dump -d "$TEST_DB" --schema-only -O -x > "$BASELINE_DUMP"
pg_dump -d "$FULL_DB" --schema-only -O -x > "$FULL_DUMP"
# pg_dump 17.10 emits a per-session random \restrict/\unrestrict token — strip
# it (and any other psql meta line) so the comparison is purely schema.
sed -i '' '/^\\restrict /d; /^\\unrestrict /d' "$BASELINE_DUMP" "$FULL_DUMP" 2>/dev/null     || sed -i '/^\\restrict /d; /^\\unrestrict /d' "$BASELINE_DUMP" "$FULL_DUMP"
if ! diff -u "$BASELINE_DUMP" "$FULL_DUMP"; then
    echo "FAIL: db/schema.sql is NOT the squashed net effect of the migrations —"
    echo "the diff above is what the baseline-pending migrations add/change vs"
    echo "the baseline. A new migration must also reflect its change in db/schema.sql"
    echo "and mark its name in the baseline seed when that folded delta is"
    echo "deliberately non-idempotent."
    rm -f "$BASELINE_DUMP" "$FULL_DUMP"
    exit 1
fi
rm -f "$BASELINE_DUMP" "$FULL_DUMP"
echo "ok convergence: schema.sql == baseline + migrations net schema"

echo "ok migrations apply + trigger smoke passed ($TEST_DB)"
