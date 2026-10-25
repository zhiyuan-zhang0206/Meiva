"""The sealed unit-capability bundle's shared helpers: the gateway and runner homes, issue / open / install, and the pure runner boot."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.host.db_authority import unit

ENDPOINT = "postgresql://meiva@10.0.0.7:6433/meiva"
MACHINE = "mini"
HUMAN = "gateway-human-secret-" + "s" * 32


def no_probe(_dsn: str) -> None:
    return None


@pytest.fixture
def gateway(tmp_path: Path, seed_write_generation: Callable[[Path], Any]) -> Path:
    home = (tmp_path / "gateway").resolve()
    home.mkdir(mode=0o700)
    seed_write_generation(home)
    return home


@pytest.fixture
def runner_home(tmp_path: Path) -> Path:
    home = (tmp_path / "runner").resolve()
    home.mkdir(mode=0o700)
    return home


def issue(
    gateway: Path,
    home: Path,
    *,
    endpoint: str = ENDPOINT,
    cluster_secret: str = HUMAN,
    ttl_s: float = 600.0,
    now: float | None = None,
) -> unit.IssuedBundle:
    identity = unit.UnitIdentity(machine=MACHINE, home=str(home))
    return unit.issue_bundle(
        gateway,
        unit=identity,
        endpoint=endpoint,
        cluster_secret=cluster_secret,
        ttl_s=ttl_s,
        now=now,
    )


def open_issued(issued: unit.IssuedBundle) -> unit.Bundle:
    return unit.open_bundle(issued.envelope, issued.transport_key)


_open = open_issued


def install(home: Path, issued: unit.IssuedBundle, **kwargs: Any) -> unit.UnitCapability:
    kwargs.setdefault("served_endpoint", ENDPOINT)
    kwargs.setdefault("probe", no_probe)
    return unit.install_bundle(home, open_issued(issued), machine=MACHINE, **kwargs)


# meiva:note
# A pure agent-runner home (no database URL in `.env`) with an installed capability.
def _install_runner(runner_home: Path, gateway: Path) -> unit.UnitCapability:
    (runner_home / ".env").write_text("MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n")
    return install(runner_home, issue(gateway, runner_home))


@pytest.fixture
def runner_capability(runner_home: Path, gateway: Path) -> unit.UnitCapability:
    """`_install_runner` and nothing else: a test passes `runner_home` and its own environment
    dict, so no process state is touched."""
    return _install_runner(runner_home, gateway)


@pytest.fixture
def runner_boot(runner_home: Path, gateway: Path) -> unit.UnitCapability:
    """`_install_runner` for a pure agent-runner home fixture."""
    return _install_runner(runner_home, gateway)
