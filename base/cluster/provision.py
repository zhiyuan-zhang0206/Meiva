"""Data-plane provisioning — per-cluster Postgres owner/db + checkpoint schema.

The idempotent ensure machinery every bring-up runs: create/re-affirm the
cluster's NOLOGIN schema owner + owned database and apply db/schema.sql acting
as that owner (`ensure_cluster_role` / `provision_database` / `drop_database`
— the inverse and half-built rollback), and the LangGraph checkpoint tables
(`ensure_checkpoint_schema`). Identities are names-as-data: callers read them
from the cluster's own `.env` URLs (`identity_from_url`) or pass the fixed
`DATA_PLANE_IDENTITY` at birth, never from a cluster name. Application
privileges belong to the capability groups and write generations in
`base.cluster.authority`, never to the owner.

Every dial is the administrator (`base.cluster.dataplane.pg_admin`): as itself for roles,
databases and extensions, and acting as the owner (`owner_session`) for the
objects the owner must own. The owner never logs in.
`base.cluster.dataplane.pg_admin` loads psycopg, so each function imports it: `base.cluster`
is on the `import meiva` path, which stays driver-free (task #3816).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

from base.cluster.authority.provision_plan import (
    CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS,
    CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION,
    CheckpointDependencyDriftError,
    CheckpointSchemaError,
    CheckpointSchemaMismatchError,
    expected_checkpoint_schema_versions,
    plan_checkpoint_schema,
    plan_database_provision,
    plan_pgvector_extension,
    verify_checkpoint_dependency_pinned,
    verify_checkpoint_schema_versions,
)
from base.cluster.checkpoint_schema import CheckpointSchema, PostgresCheckpointSchema
from base.cluster.derive import swap_db
from base.log import logger

__all__ = [
    "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS",
    "CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION",
    "CheckpointDependencyDriftError",
    "CheckpointSchemaError",
    "CheckpointSchemaMismatchError",
    "assert_checkpoint_dependency_pinned",
    "assert_checkpoint_schema_current",
    "checkpoint_schema_versions",
    "drop_database",
    "ensure_checkpoint_schema",
    "ensure_cluster_role",
    "ensure_pgvector_extension",
    "provision_database",
]


def _schema_applied(admin_url: str, target: str, *, expected_data_dir: Path | None = None) -> bool:
    """True if `target` DB has the schema fully applied — `schema_migrations` is
    the last table created by db/schema.sql, so its presence means the apply did
    not fail partway."""

    from base.cluster.dataplane.pg_admin import connect

    with connect(swap_db(admin_url, target), expected_data_dir=expected_data_dir) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations'"
        ).fetchone()
    return row is not None


def ensure_cluster_role(
    identity: str,
    *,
    base_admin_url: str,
    expected_data_dir: Path | None = None,
) -> None:
    """Create the cluster's schema owner `identity` as NOLOGIN (no password) and
    make it own the database of the same name. Idempotent — safe on every bring-up.

    `identity` is names-as-data: the caller reads it from the cluster's own
    db_url (`identity_from_url`) for an existing cluster, or passes
    `DATA_PLANE_IDENTITY` at birth — it is never derived from a cluster name, so
    prod's historical `meiva_main` keeps working until an ops rename.

    The owner never logs in: every DDL path is the administrator acting as it
    (`owner_session`), and application processes hold write-generation logins.
    An existing role is never given LOGIN or a password here. The initdb bootstrap
    superuser (the installing OS user) cannot be an owner that must lose LOGIN,
    so an identity equal to it refuses. When the database already exists,
    ownership is adopted so migrations acting as the owner own their objects.

    base_admin_url must connect as the bootstrap superuser over the owner-only
    socket to a maintenance db (e.g. `postgres`) on the same instance.

    Raises:
        RuntimeError: `identity` is the bootstrap superuser.
    """
    from psycopg import sql as pgsql

    from base.cluster.dataplane.pg_admin import connect

    with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        row = conn.execute("SELECT oid FROM pg_roles WHERE rolname = %s", (identity,)).fetchone()
        if row is not None and int(row[0]) == 10:
            raise RuntimeError(
                f"schema owner {identity!r} is the initdb bootstrap superuser; the owner "
                "must be a NOLOGIN role distinct from the OS user"
            )
        if row is None:
            conn.execute(
                pgsql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
                    " NOREPLICATION NOBYPASSRLS"
                ).format(pgsql.Identifier(identity))
            )
        db_exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (identity,)
        ).fetchone()
    if db_exists:
        _adopt_database(base_admin_url, identity, identity, expected_data_dir=expected_data_dir)


def _adopt_database(
    base_admin_url: str,
    target: str,
    owner: str,
    *,
    expected_data_dir: Path | None = None,
) -> None:
    """Make `owner` own database `target`. Idempotent — safe on every bring-up."""
    from psycopg import sql as pgsql

    from base.cluster.dataplane.pg_admin import connect

    with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        conn.execute(
            pgsql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                pgsql.Identifier(target), pgsql.Identifier(owner)
            )
        )


def provision_database(
    identity: str,
    *,
    base_admin_url: str,
    resume_initialization: bool = False,
    expected_data_dir: Path | None = None,
) -> bool:
    """Atomically provision a cluster's Postgres database AND its owning role:
    ensure the NOLOGIN owner `identity`, CREATE DATABASE `identity` OWNED BY it,
    and apply db/schema.sql acting as that role (`owner_session`) so every object
    is role-owned. `identity` is the shared db/role identifier (names-as-data —
    `DATA_PLANE_IDENTITY` at birth). base_admin_url must connect as the
    bootstrap superuser to a maintenance db (`postgres`) on the same Postgres.

    Idempotent for a fully-provisioned DB (the role is re-affirmed + ownership
    adopted, then it returns). If the DB exists but its schema is incomplete (a
    prior apply crashed mid-way), this raises rather than silently treating it as
    ready. Provisioning is atomic: if schema apply fails on a freshly-created DB,
    the DB is dropped so a retry starts clean.

    Returns:
        True only when this call created the database; False when it adopted an
        already-provisioned database. Birth uses this exact database provenance
        to decide whether a later checkpoint-setup failure may drop the database.

    Raises:
        RuntimeError: the DB exists but schema_migrations is missing (half-provisioned).
    """
    from psycopg import sql as pgsql

    from base.cluster.dataplane.pg_admin import connect, owner_session

    ensure_cluster_role(
        identity, base_admin_url=base_admin_url, expected_data_dir=expected_data_dir
    )
    with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (identity,)
        ).fetchone()
    db_exists = exists is not None
    applied = (
        _schema_applied(base_admin_url, identity, expected_data_dir=expected_data_dir)
        if db_exists
        else False
    )
    table_count: int | None = None
    if db_exists and not applied and resume_initialization:
        with connect(
            swap_db(base_admin_url, identity), expected_data_dir=expected_data_dir
        ) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
            ).fetchone()
        table_count = None if row is None else int(row[0])
    action = plan_database_provision(
        identity,
        db_exists=db_exists,
        schema_applied=applied,
        resume_initialization=resume_initialization,
        public_table_count=table_count,
    )
    if action == "noop":
        return False

    if action == "create_and_apply":
        with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
            conn.execute(
                pgsql.SQL("CREATE DATABASE {} OWNER {}").format(
                    pgsql.Identifier(identity), pgsql.Identifier(identity)
                )
            )
    schema_sql = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()
    try:
        # Apply the schema as the administrator ACTING AS the cluster role, so
        # every object is owned by the role, not the bootstrap superuser — the
        # role must own them for later migrations and its grants. The role never
        # logs in here. schema.sql is a trusted multi-statement script read from
        # disk; same pattern as base/deploy/schema/migrations.py applying a body.
        with owner_session(
            base_admin_url,
            database=identity,
            owner=identity,
            expected_data_dir=expected_data_dir,
            autocommit=True,
        ) as conn:
            conn.execute(schema_sql)  # type: ignore[arg-type]
    except Exception:
        # Drop the half-built DB so the next provision attempt starts clean
        # rather than tripping the "exists but incomplete" guard above.
        if not exists:
            drop_database(
                identity, base_admin_url=base_admin_url, expected_data_dir=expected_data_dir
            )
        raise
    return not bool(exists)


def ensure_pgvector_extension(
    identity: str, *, base_admin_url: str, expected_data_dir: Path | None = None
) -> None:
    """Pre-create the pgvector extension in the cluster database with the
    bootstrap-superuser connection (`base_admin_url`), so the application
    logins never need to: pgvector's `vector.control` ships without
    `trusted = true`, which makes `CREATE EXTENSION` superuser-only by
    Postgres' own policy (deliberately not overridden on the injected control
    file). Idempotent — normal gateway start prepares it before the pgvector
    memory table, so a cluster picks the extension up on its next start.

    A Postgres that does not carry the extension binaries (a remote-managed
    plane, a brew/apt install without the pgvector package, or a vendored
    tree from before the injection landed) is a silent no-op — the memory
    indexer's startup preflight owns that failure surface with its actionable
    message. Deliberately not a migration: migrations must apply against
    Postgres installations that have no pgvector at all.

    An unreachable admin connection is a no-op too (logged, not raised): the
    ensure re-runs on every bring-up, so a transient dead socket retries next
    start, and the migrations step right behind it is the loud failure path
    when the data plane is genuinely gone.
    """
    import psycopg
    from psycopg import sql as pgsql

    from base.cluster.dataplane.pg_admin import connect

    try:
        with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
            available = conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
        ddl = plan_pgvector_extension(extension_available=available is not None)
        if ddl is None:
            return
        with connect(
            swap_db(base_admin_url, identity), expected_data_dir=expected_data_dir, autocommit=True
        ) as conn:
            conn.execute(pgsql.SQL("CREATE EXTENSION IF NOT EXISTS vector"))
    except psycopg.OperationalError as exc:
        logger.warning(
            "[pgvector] pre-create skipped: cluster Postgres unreachable over the "
            "admin connection ({}) — re-attempted on the next bring-up",
            exc,
        )


def drop_database(
    identity: str, *, base_admin_url: str, expected_data_dir: Path | None = None
) -> None:
    """DROP DATABASE `identity` + its owning role of the same name — the inverse
    of provision_database, and its half-built rollback. base_admin_url must
    connect as the bootstrap superuser (owner-only socket, `peer`) to a
    maintenance db (`postgres`) on the same Postgres.

    Idempotent (IF EXISTS). The caller is responsible for there being no live
    connections to the target (in prod `meiva cluster destroy` runs after the
    cluster is stopped); this does not force-terminate backends. The role is
    dropped after the database so it owns nothing and the drop cannot fail on a
    dependency.
    """
    from psycopg import sql as pgsql

    from base.cluster.dataplane.pg_admin import connect

    with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        conn.execute(pgsql.SQL("DROP DATABASE IF EXISTS {}").format(pgsql.Identifier(identity)))
        conn.execute(pgsql.SQL("DROP ROLE IF EXISTS {}").format(pgsql.Identifier(identity)))


# meiva:note
# Fail before any DB work when the dependency schema contract drifted.
#
# `tracked_migrations` is the caller's set of git-tracked Meiva migration names
# (`base.deploy.schema.migration_layout.required_migration_set()`): `cluster`
# sits below the schema owner, so the caller supplies it. Every `manifest` entry
# must name one of them, and the manifest must approve exactly the
# `upstream_migrations` the installed dependency ships. The composition root
# passes `CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS` and
# `PostgresCheckpointSchema().migrations`.
def assert_checkpoint_dependency_pinned(
    tracked_migrations: Collection[str],
    *,
    manifest: Mapping[int, str],
    upstream_migrations: Sequence[str],
) -> None:
    verify_checkpoint_dependency_pinned(
        tracked_migrations,
        manifest=manifest,
        upstream_migrations=upstream_migrations,
    )


def checkpoint_schema_versions(
    db_url: str, *, expected_data_dir: Path | None = None
) -> frozenset[int] | None:
    """Return the complete applied set, or ``None`` when no schema exists."""

    from base.cluster.dataplane.pg_admin import connect

    with connect(db_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        table = conn.execute("SELECT to_regclass('public.checkpoint_migrations')").fetchone()
        if table is None or table[0] is None:
            return None
        rows = conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    return frozenset(int(row[0]) for row in rows)


def assert_checkpoint_schema_current(db_url: str, *, expected_data_dir: Path | None = None) -> None:
    """Require the exact approved checkpoint migration set, without mutation.

    Every start role calls this after Meiva migrations.  A pure runner therefore
    detects behind, ahead, empty, or internally-gapped checkpoint state without
    acquiring DDL capability.  Ahead is also refused: upstream gives no old-
    runtime/new-schema compatibility guarantee.
    """
    expected = expected_checkpoint_schema_versions(
        PostgresCheckpointSchema().migrations, CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS
    )
    actual = checkpoint_schema_versions(db_url, expected_data_dir=expected_data_dir)
    verify_checkpoint_schema_versions(actual, expected)


def ensure_checkpoint_schema(
    identity: str,
    *,
    base_admin_url: str,
    schema: CheckpointSchema,
    database_created: bool = False,
    resume_partial: bool = False,
    expected_data_dir: Path | None = None,
) -> None:
    from base.cluster.dataplane.pg_admin import owner_conninfo

    # meiva:note
    # Act as the schema owner and retain the validated native connection
    # throughout setup; PostgresSaver must not open an unchecked second dial.
    owner_dsn = owner_conninfo(base_admin_url, database=identity, owner=identity)
    expected = expected_checkpoint_schema_versions(
        schema.migrations, CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS
    )
    actual = checkpoint_schema_versions(owner_dsn, expected_data_dir=expected_data_dir)
    action = plan_checkpoint_schema(
        actual,
        expected,
        database_created=database_created,
        resume_partial=resume_partial,
    )
    if action == "noop":
        return
    if action == "verify_only":
        assert_checkpoint_schema_current(owner_dsn, expected_data_dir=expected_data_dir)
        return
    try:
        schema.setup(
            base_admin_url=base_admin_url, identity=identity, expected_data_dir=expected_data_dir
        )
        assert_checkpoint_schema_current(owner_dsn, expected_data_dir=expected_data_dir)
    except Exception:
        if database_created:
            drop_database(
                identity, base_admin_url=base_admin_url, expected_data_dir=expected_data_dir
            )
        raise
