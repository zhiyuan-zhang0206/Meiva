"""Data-plane startup ordering, authority and prerequisite contracts."""

import dataclasses
import json
import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from base import cluster
from base.cluster.machine import MachineIdentity
from base.db.tests.fakes import patch_database
from cli import start_identity
from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import cluster_record
from cli.database import OperatorDatabaseFactory
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_machine as command_machine


def _rec() -> cluster.ClusterRecord:
    return cluster_record({"gateway": 23000, "postgres": 23011, "redis": 23012}, created_at="x")


class _Ledger:
    def __init__(self, *, active: bool) -> None:
        self.owner = "meiva"
        self.groups = "groups"
        self.active = SimpleNamespace(number=0) if active else None
        self.generation = SimpleNamespace(number=0)


# meiva:note
# Recorded effects of `complete_gateway_data_plane` and the fake start state.
#
# The home is a real gateway home whose start intent says how far its birth got: an
# ordinary start finds it `provisioned`; `birth()` takes it back to `configured`, as
# `meiva init` leaves it before the first start provisions the data plane.
class _Plane:
    def __init__(self, config: DataPlaneConfig, home: Path) -> None:
        self.config = config
        self.home = home
        self.calls: list[str] = []
        self.state = {"active": True, "verify_fails": False}

    def phase(self) -> str:
        intent = start_identity.read_intent(self.home)
        assert intent is not None
        return intent["phase"]

    def birth(self) -> None:
        path = self.home / start_identity.INTENT_NAME
        intent = json.loads(path.read_text())
        intent["phase"] = "configured"
        path.write_text(json.dumps(intent, sort_keys=True) + "\n")
        self.state["active"] = False


@pytest.fixture
def plane(
    monkeypatch: pytest.MonkeyPatch, data_plane_config: DataPlaneConfig, unit_home: Path
) -> _Plane:
    """Every effect of `complete_gateway_data_plane`, recorded in order."""
    from base.cluster import authority
    from base.host.db_authority import delivery, ledger
    from cli.commands.converge import health_preflight
    from cli.commands.data_plane import bringup

    start_identity.prepare_identity(
        start_identity.IdentityInput(
            unit_home,
            unit_home.parent / "checkout",
            frozenset({"gateway"}),
            {"MEIVA_MACHINE_NAME": "plane"},
        )
    )
    start_identity.mark_phase(unit_home, "provisioned")
    recorded = _Plane(
        dataclasses.replace(
            data_plane_config,
            db_url="postgresql://meiva@127.0.0.1:6433/meiva",
            redis_url="redis://127.0.0.1:6380/0",
        ),
        unit_home,
    )
    calls, state = recorded.calls, recorded.state

    def record(name: str, result: object = None) -> object:
        def effect(*_a: object, **_kw: object) -> object:
            calls.append(name)
            return result

        return effect

    @contextmanager
    def admin(*_a: object) -> Generator[str]:
        yield "admin-connection"

    monkeypatch.setattr(bringup, "prepare_memory_vectors", record("memory-vectors"))
    monkeypatch.setattr(bringup, "admin_session", admin)
    monkeypatch.setattr(bringup, "db_endpoint", lambda: "postgresql://meiva@127.0.0.1:6433/meiva")
    monkeypatch.setattr(bringup, "ensure_pooler", record("pooler"))
    monkeypatch.setattr(bringup, "prove_generation_logins", record("prove-logins"))
    monkeypatch.setattr(bringup, "adopt_gateway_login", record("adopt"))
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(cluster, "ensure_pgvector_extension", record("extension"))

    def fake_ledger(_home: object) -> _Ledger:
        return _Ledger(active=state["active"])

    monkeypatch.setattr(ledger, "load_ledger", fake_ledger)
    monkeypatch.setattr(ledger, "require_ledger", fake_ledger)
    monkeypatch.setattr(authority, "ensure_groups", record("groups"))
    monkeypatch.setattr(authority, "ensure_monitor", record("monitor"))
    monkeypatch.setattr(ledger, "create_ledger", record("ledger"))
    monkeypatch.setattr(delivery, "ensure_pooler_admin", record("pooler-admin"))
    monkeypatch.setattr(authority, "mint_generation", record("mint"))
    monkeypatch.setattr(authority, "check_invariant", record("invariant"))

    def verify(*_a: object, **_kw: object) -> None:
        calls.append("verify")
        if state["verify_fails"]:
            raise RuntimeError("generation 0 does not verify")

    monkeypatch.setattr(authority, "verify_generation", verify)

    def activate(*_a: object) -> None:
        calls.append("activate")
        state["active"] = True

    monkeypatch.setattr(ledger, "activate", activate)
    monkeypatch.setattr(health_preflight, "probe_redis", record("redis-probe"))
    calls.append("start")
    return recorded


def test_ordinary_start_regrants_and_checks_before_the_pooler(
    plane: _Plane,
    operator_database: OperatorDatabaseFactory,
    *,
    command_machine: MachineIdentity,
) -> None:
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    complete_gateway_data_plane(
        plane.config, database_factory=operator_database, machine=command_machine
    )
    assert plane.calls == [
        "start",
        "extension",
        "memory-vectors",
        "groups",
        "monitor",
        "invariant",
        "pooler",
        "prove-logins",
        "adopt",
        "redis-probe",
    ]
    assert plane.phase() == "provisioned"


def test_birth_mints_generation_zero_and_activates_after_the_pooler_proof(
    plane: _Plane,
    operator_database: OperatorDatabaseFactory,
    *,
    command_machine: MachineIdentity,
) -> None:
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    plane.birth()
    assert plane.phase() == "configured"
    complete_gateway_data_plane(
        plane.config, database_factory=operator_database, machine=command_machine
    )
    assert plane.calls == [
        "start",
        "extension",
        "memory-vectors",
        "groups",
        "monitor",
        "ledger",
        "pooler-admin",
        "mint",
        "pooler",
        "prove-logins",
        "verify",
        "activate",
        "adopt",
        "redis-probe",
    ]
    assert plane.phase() == "provisioned", "the birth is journaled once the data plane is up"


def test_release_readiness_performs_no_schema_or_grant_writes(
    plane: _Plane,
    operator_database: OperatorDatabaseFactory,
    *,
    command_machine: MachineIdentity,
) -> None:
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    complete_gateway_data_plane(
        plane.config,
        refresh_schema=False,
        database_factory=operator_database,
        machine=command_machine,
    )
    assert plane.calls == [
        "start",
        "invariant",
        "pooler",
        "prove-logins",
        "adopt",
        "redis-probe",
    ]


def test_invariant_violation_never_starts_the_pooler(
    plane: _Plane,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    *,
    command_machine: MachineIdentity,
) -> None:
    from base.cluster import authority
    from base.host.db_authority import model
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    def fail(*_a: object, **_kw: object) -> None:
        raise model.CatalogRefusedError(("foreign grant",))

    monkeypatch.setattr(authority, "check_invariant", fail)
    with pytest.raises(model.CatalogRefusedError, match="foreign grant"):
        complete_gateway_data_plane(
            plane.config, database_factory=operator_database, machine=command_machine
        )
    assert plane.calls == ["start", "extension", "memory-vectors", "groups", "monitor"]


# meiva:note
# The birth is journaled only once the data plane is up: a failed check leaves the
# home `configured`, so the next start births it again.
def test_a_birth_that_fails_its_generation_check_stays_configured(
    plane: _Plane,
    operator_database: OperatorDatabaseFactory,
    *,
    command_machine: MachineIdentity,
) -> None:
    from cli.commands.data_plane.bringup import complete_gateway_data_plane

    plane.birth()
    plane.state["verify_fails"] = True
    with pytest.raises(RuntimeError, match="does not verify"):
        complete_gateway_data_plane(
            plane.config, database_factory=operator_database, machine=command_machine
        )
    assert plane.phase() == "configured"


class _Authority:
    """Records the owner-authority dial `prepare_memory_vectors` must use."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @contextmanager
    def session(self) -> Generator[str]:
        self._calls.append("owner-session")
        yield "owner-connection"


def test_memory_vectors_prepared_as_owner_only_for_pgvector(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    data_plane_config: DataPlaneConfig,
) -> None:
    """The pgvector table is start-time DDL through the owner authority at the
    provider's dimension; any other backend dials nothing."""
    from base.cluster.dataplane import pg_admin
    from cli.commands.data_plane.bringup import prepare_memory_vectors
    from services.derived.memory_indexer.backends import pgvector
    from services.derived.memory_indexer.embeddings import factory

    calls: list[str] = []

    def owner_authority(db_url: str, *, remote_plane: bool) -> _Authority:
        assert db_url == data_plane_config.db_url
        assert not remote_plane
        return _Authority(calls)

    monkeypatch.setattr(pg_admin, "local_owner_authority", owner_authority)

    def prepare(conn: str, dim: int) -> None:
        calls.append(f"prepare:{conn}:{dim}")

    monkeypatch.setattr(pgvector, "prepare_table", prepare)

    def descriptor(name: str) -> SimpleNamespace:
        calls.append(f"descriptor:{name}")
        return SimpleNamespace(dim=3072)

    monkeypatch.setattr(factory, "get_descriptor", descriptor)

    data_plane_config = dataclasses.replace(data_plane_config, memory_search_backend="numpy")
    prepare_memory_vectors(data_plane_config, database_factory=operator_database)
    assert calls == []

    data_plane_config = dataclasses.replace(data_plane_config, memory_search_backend="pgvector")
    prepare_memory_vectors(data_plane_config, database_factory=operator_database)
    assert calls == [
        f"descriptor:{data_plane_config.embedding_backend}",
        "owner-session",
        "prepare:owner-connection:3072",
    ]


def test_remote_plane_prepares_memory_vectors_through_its_provider_url(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    data_plane_config: DataPlaneConfig,
) -> None:
    """A remote-managed plane has no local owner authority; its provider URL
    carries the table DDL, exactly as it carries the plane's migrations."""
    from base.cluster.dataplane import pg_admin
    from cli.commands.data_plane.bringup import prepare_memory_vectors
    from services.derived.memory_indexer.backends import pgvector
    from services.derived.memory_indexer.embeddings import factory

    calls: list[str] = []

    @contextmanager
    def provider(**kwargs: object) -> Generator[str]:
        calls.append(f"provider:{kwargs}")
        yield "provider-connection"

    def prepare(conn: str, dim: int) -> None:
        calls.append(f"prepare:{conn}:{dim}")

    data_plane_config = dataclasses.replace(
        data_plane_config,
        db_url="postgresql://owner@db.example/meiva",
        memory_search_backend="pgvector",
    )
    patch_database(monkeypatch, connect=provider)

    def refuse_local_owner(_db_url: str, *, remote_plane: bool) -> NoReturn:
        pytest.fail(f"remote plane must not resolve local authority (remote={remote_plane})")

    monkeypatch.setattr(pg_admin, "local_owner_authority", refuse_local_owner)
    monkeypatch.setattr(pgvector, "prepare_table", prepare)

    def descriptor(name: str) -> SimpleNamespace:
        assert name == data_plane_config.embedding_backend
        return SimpleNamespace(dim=768)

    monkeypatch.setattr(factory, "get_descriptor", descriptor)

    prepare_memory_vectors(data_plane_config, database_factory=operator_database)

    assert calls == ["provider:{'direct': True}", "prepare:provider-connection:768"]


@pytest.mark.parametrize(
    ("secret", "missing", "reason"),
    [
        ("bearer-only", "redis_admin_password", "no conversion exists"),
        ("bearer-only", "redis_password", "no conversion exists"),
        ("", "redis_admin_password", "no conversion exists"),
        ("", "redis_password", "no conversion exists"),
    ],
)
def test_storage_refuses_missing_credentials_before_effects(
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
    missing: str,
    reason: str,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """Redis always authenticates, so an empty bearer does not excuse missing
    Redis credentials: a home born without them is refused before any native
    effect. Postgres needs no credential here: its
    administrator is the OS user over the owner-only socket."""
    credentials = {"redis_admin_password": "admin-value", "redis_password": "runtime-value"}
    credentials[missing] = ""
    data_plane_config = dataclasses.replace(
        data_plane_config, redis_admin_password=credentials["redis_admin_password"]
    )
    monkeypatch.setattr(
        _ci,
        "start_pg",
        lambda *_a: pytest.fail("storage started before validation"),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    with pytest.raises(ValueError, match=reason):
        _ci.ensure_cluster_storage(
            data_plane_config,
            pg_port=5433,
            redis_port=6380,
            redis_user="meiva",
            redis_password=credentials["redis_password"],
            retained_children=retained_children,
            machine=command_machine,
        )


@pytest.fixture
def retained_children() -> list[subprocess.Popen[bytes]]:
    return []
