# meiva:note
# What a gateway's data-plane steps do, as values.
#
# `admit_gateway_storage` decides how the bring-up step proceeds (refuse, probe a remote-managed
# plane, or start the owned instance); `plan_completion` orders the steps that grant the final
# schema, establish the write generation, start the pooler and verify consumer credentials.
# The shell in `bringup.py` observes the few facts these read and executes each step in order,
# stopping at the first failure.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ActivateGeneration",
    "Admission",
    "AdmitGeneration",
    "AdoptLogin",
    "BirthGeneration",
    "CompletionFacts",
    "CompletionStep",
    "EnsureExtension",
    "EnsurePooler",
    "InAdminSession",
    "MarkProvisioned",
    "PrepareVectors",
    "ProbeConsumerDatabases",
    "ProbeRedis",
    "ProbeRemotePlane",
    "ProveLogins",
    "RecordedPorts",
    "Refuse",
    "RefuseLegacy",
    "ReportAdmitted",
    "StartLocalStorage",
    "admit_gateway_storage",
    "legacy_home_refusal",
    "plan_completion",
    "require_storage_credentials",
]


# meiva:note
# Why an ordinary start refuses a home born before the data plane always authenticated:
# nothing converts it.
def legacy_home_refusal(home: Path) -> str:
    return (
        f"home {home} has no database authority ledger (born before the data plane "
        "always authenticated) and no conversion exists; re-birth it as a new home"
    )


# meiva:note
# The storage-level refusal of a home born without Redis credentials: nothing converts it.
#
# Raises ValueError, before any native effect, when the Redis admin or runtime password is
# empty. Postgres needs none: its administrator is the OS user over the owner-only socket.
def require_storage_credentials(admin_password: str, runtime_password: str) -> None:
    if not (admin_password and runtime_password):
        raise ValueError(
            "Redis always authenticates, but this home has no generated Redis credentials "
            "(MEIVA_REDIS_ADMIN_PASSWORD / MEIVA_REDIS_PASSWORD): it was born before Redis "
            "always authenticated and no conversion exists; re-birth it as a new home."
        )


# meiva:note
# The start refuses with `message` (printed to stderr, exit 1).
@dataclass(frozen=True)
class Refuse:
    message: str


# meiva:note
# The URLs name another host: there is no local instance, only reachability to verify.
@dataclass(frozen=True)
class ProbeRemotePlane: ...


# meiva:note
# The two storage ports the home's registry record reserved.
@dataclass(frozen=True)
class RecordedPorts:
    postgres: int
    redis: int


# meiva:note
# Bring up the home's own Postgres and Redis on the ports its record reserved.
@dataclass(frozen=True)
class StartLocalStorage:
    ports: RecordedPorts


Admission = Refuse | ProbeRemotePlane | StartLocalStorage


# meiva:note
# How the gateway's data-plane start proceeds.
#
# A home with no registry record cannot say which ports are its own. A remote-managed plane
# degrades to a reachability probe of its URLs. A local plane whose home has no database
# authority ledger (born before the data plane always authenticated) is refused before any
# native effect (hba rewrite, migrations), never converted here.
def admit_gateway_storage(
    *, home: Path, ports: RecordedPorts | None, remote: bool, legacy_home: bool
) -> Admission:
    if ports is None:
        return Refuse(
            f"  ✗ no registry record for home {home} — cannot bring up its "
            "data plane. Run `meiva init` for this home first."
        )
    if remote:
        return ProbeRemotePlane()
    if legacy_home:
        return Refuse(f"  ✗ {legacy_home_refusal(home)}")
    return StartLocalStorage(ports)


# meiva:note
# What `plan_completion` reads about the home.
#
# `birth` is the home's first start (the journal says it was only `configured`);
# `ledger_present` whether its database authority ledger exists.
@dataclass(frozen=True)
class CompletionFacts:
    home: Path
    remote: bool
    refresh_schema: bool
    birth: bool
    ledger_present: bool


# meiva:note
# Create the pgvector extension in the home's database.
@dataclass(frozen=True)
class EnsureExtension: ...


# meiva:note
# Create or rebuild the pgvector memory table when that backend is selected.
@dataclass(frozen=True)
class PrepareVectors: ...


# meiva:note
# The home has no ledger and is not being born: stop with `message`.
@dataclass(frozen=True)
class RefuseLegacy:
    message: str


# meiva:note
# Birth: groups, monitor, ledger, generation 0.
@dataclass(frozen=True)
class BirthGeneration: ...


# meiva:note
# Ordinary start: re-grant after migrations when `refresh`, converge the monitor, and hold the
# catalog invariant.
@dataclass(frozen=True)
class AdmitGeneration:
    refresh: bool


# meiva:note
# Serve exactly the generation through the owned pooler (restarted only when its bytes change).
@dataclass(frozen=True)
class EnsurePooler: ...


# meiva:note
# Both logins of the generation answer `SELECT 1` through the consumer endpoint.
@dataclass(frozen=True)
class ProveLogins: ...


# meiva:note
# Verify and activate the generation when the ledger has none active yet.
@dataclass(frozen=True)
class ActivateGeneration: ...


# meiva:note
# Run `steps` on the OS-user administrator's connection to the home's own database.
@dataclass(frozen=True)
class InAdminSession:
    steps: tuple[CompletionStep, ...]


# meiva:note
# Point this operator process at the home's active gateway login.
@dataclass(frozen=True)
class AdoptLogin: ...


# meiva:note
# Say which write generation is admitted.
@dataclass(frozen=True)
class ReportAdmitted: ...


# meiva:note
# The consumer databases (the access URL and the runner login) answer on a remote plane.
@dataclass(frozen=True)
class ProbeConsumerDatabases: ...


# meiva:note
# The consumer Redis answers.
@dataclass(frozen=True)
class ProbeRedis: ...


# meiva:note
# Journal that the data plane is provisioned.
@dataclass(frozen=True)
class MarkProvisioned: ...


CompletionStep = (
    EnsureExtension
    | PrepareVectors
    | RefuseLegacy
    | BirthGeneration
    | AdmitGeneration
    | EnsurePooler
    | ProveLogins
    | ActivateGeneration
    | InAdminSession
    | AdoptLogin
    | ReportAdmitted
    | ProbeConsumerDatabases
    | ProbeRedis
    | MarkProvisioned
)


# meiva:note
# The ordered steps that complete a gateway's data plane after its migrations.
#
# A remote-managed plane prepares the memory table through its provider URL and verifies its
# consumers. A local plane first (on a schema refresh) gets its extension and memory table, then
# refuses a home without a ledger, then inside one administrator session births or admits the
# write generation, starts the pooler serving exactly it, proves both logins and activates it.
# The Redis consumer check and the journal come last, so a failed check leaves the home
# `configured` and the next start births it again.
def plan_completion(facts: CompletionFacts) -> tuple[CompletionStep, ...]:
    refresh = facts.refresh_schema
    tail: tuple[CompletionStep, ...] = (ProbeRedis(), MarkProvisioned())
    if facts.remote:
        vectors: tuple[CompletionStep, ...] = (PrepareVectors(),) if refresh else ()
        return (*vectors, ProbeConsumerDatabases(), *tail)
    prepare: tuple[CompletionStep, ...] = (EnsureExtension(), PrepareVectors()) if refresh else ()
    if not facts.birth and not facts.ledger_present:
        return (*prepare, RefuseLegacy(legacy_home_refusal(facts.home)))
    generation = BirthGeneration() if facts.birth else AdmitGeneration(refresh=refresh)
    return (
        *prepare,
        InAdminSession((generation, EnsurePooler(), ProveLogins(), ActivateGeneration())),
        AdoptLogin(),
        ReportAdmitted(),
        *tail,
    )
