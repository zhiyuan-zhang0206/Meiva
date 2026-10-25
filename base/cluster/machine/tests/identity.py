"""Explicit machine owners; each consumer supplies its fixture to its real root."""

import pathlib
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

import psycopg
import pytest

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot

__all__ = [
    "MachineIdentitySetter",
    "as_machine",
    "machine_identity",
    "runner_unit",
    "set_machine_identity",
]


class MachineIdentitySetter(Protocol):
    """A test's own identity, returned so its consumer can borrow it explicitly."""

    def __call__(self, *, role: str, name: str | None = None) -> ConfiguredMachineIdentity: ...


@pytest.fixture
def machine_identity(process_config: ConfigBoot) -> ConfiguredMachineIdentity:
    """Own independent field caches; construction does not resolve configuration."""
    return ConfiguredMachineIdentity(process_config)


@pytest.fixture
def set_machine_identity(machine_identity: ConfiguredMachineIdentity) -> MachineIdentitySetter:
    """Set this test's owner and return it; other roots keep their own identities."""

    def configure(*, role: str, name: str | None = None) -> ConfiguredMachineIdentity:
        if name is None:
            machine_identity.set(role=role)
        else:
            machine_identity.set(role=role, name=name)
        return machine_identity

    return configure


@pytest.fixture
def runner_unit(
    db_conn: psycopg.Connection, machine_identity: ConfiguredMachineIdentity
) -> ConfiguredMachineIdentity:
    """Supply an agent-runner owner after the database fixture's per-test cleanup."""
    _ = db_conn
    machine_identity.set(role="agent-runner")
    return machine_identity


@pytest.fixture
def as_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[pathlib.Path], AbstractContextManager[ConfiguredMachineIdentity]]:
    """Borrow a fresh machine owner for each real home in a cross-home test."""

    @contextmanager
    def _enter(home: pathlib.Path) -> Generator[ConfiguredMachineIdentity]:
        home.mkdir(parents=True, exist_ok=True)
        with monkeypatch.context() as m:
            m.setenv("MEIVA_HOME", str(home))
            config = ConfigBoot()
            config.read_process_environment()
            machine = ConfiguredMachineIdentity(config)
            machine.set(name=home.name)
            yield machine

    return _enter
