# meiva:note
# The ordered preparation a start performs before anything launches.
#
# A start with no live application root prepares the host and its storage; a start that finds
# a live root only inspects, because pending schema changes need a stopped writer boundary and
# never run as a repeat-start effect. What happens, and in which order, is the plan returned
# here; the start shell executes each step and stops at the first one that fails.

from __future__ import annotations

from collections.abc import Collection
from enum import Enum

__all__ = [
    "ColdStep",
    "MigrationAuthority",
    "SchemaCheck",
    "migration_authority",
    "plan_cold_start",
    "plan_schema_checks",
]


# meiva:note
# One preparation step; the value names its phase in the start trace.
class ColdStep(Enum):
    CONVERGE = "converge host"
    DATA_PLANE = "data plane (pg + redis)"
    GATEWAY_SCHEMA = "gateway schema"
    SKIP_LOCAL_SERVICES = "local services"
    MIGRATIONS = "migrations"
    POOLER = "pgbouncer"
    SCHEMA_CHECK = "schema check"
    ADOPT_EXTENSIONS = "extensions adopt"
    MATERIALIZE_EXTENSIONS = "extensions materialize"


# meiva:note
# What a start verifies about the database it is about to launch against.
class SchemaCheck(Enum):
    MEIVA = "schema check (post-admission)"
    CHECKPOINT = "checkpoint schema (live root)"


# meiva:note
# The preparation of a start with no live root.
#
# The host converges first. A gateway then brings up its own Postgres and Redis, prepares
# the gateway schema, applies migrations, completes the pooler and grants, and checks the
# schema; a runner-only host uses the central node's database and skips that storage.
# The cluster's installed extensions land last, after the schema check on purpose: the
# registry they read exists only once the data plane is up and known-current. Adoption
# precedes materialization so one pass leaves the machine and the cluster agreeing.
def plan_cold_start(roles: Collection[str]) -> tuple[ColdStep, ...]:
    if "gateway" in roles:
        storage = (ColdStep.DATA_PLANE, ColdStep.GATEWAY_SCHEMA)
        pooler = (ColdStep.POOLER,)
    else:
        storage = (ColdStep.SKIP_LOCAL_SERVICES,)
        pooler = ()
    return (
        ColdStep.CONVERGE,
        *storage,
        ColdStep.MIGRATIONS,
        *pooler,
        ColdStep.SCHEMA_CHECK,
        ColdStep.ADOPT_EXTENSIONS,
        ColdStep.MATERIALIZE_EXTENSIONS,
    )


# meiva:note
# The database checks every admitted start performs before launching.
#
# A live root is only inspected: it additionally verifies the checkpoint schema, since no
# cold preparation just brought it current.
def plan_schema_checks(*, live: bool) -> tuple[SchemaCheck, ...]:
    if live:
        return (SchemaCheck.MEIVA, SchemaCheck.CHECKPOINT)
    return (SchemaCheck.MEIVA,)


# meiva:note
# Who may change the schema, and over which connection.
class MigrationAuthority(Enum):
    # meiva:note
    # The administrator acting as the schema owner over the home's own socket: objects stay
    # owner-owned and the owner's own login is never used. It survives a transaction pooler
    # because the applier holds a session advisory lock for its whole loop.
    OWNER_SOCKET = "owner socket"
    # meiva:note
    # The ordinary access URL: a plane this home does not own is dialed as every consumer
    # does, and only ever finds nothing pending unless the mutation authority admits it.
    ACCESS_URL = "access url"


# meiva:note
# A plane this home owns migrates over its owner socket; a remote-managed plane never has
# a local owner authority and keeps the provider's explicit URL authority.
def migration_authority(*, remote_plane: bool) -> MigrationAuthority:
    return MigrationAuthority.ACCESS_URL if remote_plane else MigrationAuthority.OWNER_SOCKET
