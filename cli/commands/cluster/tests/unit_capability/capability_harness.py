# meiva:note
# The pieces a unit-capability test assembles from real resources.
#
# A gateway home whose private ledger holds an active write generation, a bundle sealed from it
# for one unit, the database login that bundle carries (a role of the suite's throwaway
# Postgres, so the install's login probe connects for real), an initialized runner
# home, and the bootstrap answer a gateway serves. The gateway itself is `gateway_server`'s
# scripted listener.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from base.cluster.authority.tests.runner_logins import grant_runner_login
from base.host.db_authority.delivery import active_generation
from base.host.db_authority.ledger import read_secret
from base.host.db_authority.unit import UnitIdentity, issue_bundle
from base.host.net.tests.local_http import closed_port_url
from base.host.net.url_secret import credential_free
from cli import start_identity
from cli.commands.cluster.tests.gateway_server import Reply

__all__ = [
    "BOOTSTRAP",
    "CLUSTER_SECRET",
    "MACHINE",
    "SealedBundle",
    "bootstrap_reply",
    "database_endpoint",
    "gateway_home",
    "initialized_runner",
    "missing_database_endpoint",
    "refused_endpoint",
    "runner_api_token",
    "runner_home",
    "seal_bundle",
]

MACHINE = "mini"
BOOTSTRAP = ("GET", "/api/bootstrap")

# meiva:note
# A literal of the throwaway gateway these tests seal bundles for; it never leaves the box.
CLUSTER_SECRET = "gateway-human-secret-" + "s" * 32


# meiva:note
# A bundle on disk and the transport key that opens it.
@dataclass(frozen=True)
class SealedBundle:
    path: Path
    transport_key: str


@pytest.fixture
def gateway_home(tmp_path: Path, seed_write_generation: Callable[[Path], object]) -> Path:
    """A gateway home whose private ledger holds an active write generation."""
    home = (tmp_path / "gateway").resolve()
    home.mkdir(mode=0o700)
    seed_write_generation(home)
    return home


@pytest.fixture
def runner_home(tmp_path: Path) -> Path:
    """A runner unit's home directory, as the gateway's bundles name it."""
    home = (tmp_path / "runner").resolve()
    home.mkdir(mode=0o700)
    return home


@pytest.fixture
def database_endpoint(db_url: str, gateway_home: Path) -> str:
    """The credential-free endpoint of the suite's Postgres, serving the ledger's runner login.

    The login is a role the cluster accepts, so the install's probe connects as the
    generation's runner for real.
    """
    login = read_secret(gateway_home, active_generation(gateway_home)).roles.runner
    grant_runner_login(db_url, owner="meiva_citest", login=login.name, password=login.password)
    return credential_free(db_url)


# meiva:note
# The endpoint of a database nothing listens on, for a test whose join fails before any login
# is probed.
def refused_endpoint() -> str:
    return f"postgresql://meiva@127.0.0.1:{urlsplit(closed_port_url()).port}/meiva"


# meiva:note
# The suite's Postgres, asked for a database it does not have: the cluster answers and refuses
# the install's login probe, whatever roles the suite's server holds.
def missing_database_endpoint(db_url: str) -> str:
    return credential_free(db_url).rpartition("/")[0] + "/no_such_database"


# meiva:note
# Seal the gateway home's active generation for `machine` at `unit_home`, bound to `endpoint`,
# and write it to `path`.
def seal_bundle(
    gateway: Path,
    path: Path,
    *,
    endpoint: str,
    unit_home: Path,
    machine: str = MACHINE,
) -> SealedBundle:
    issued = issue_bundle(
        gateway,
        unit=UnitIdentity(machine=machine, home=str(unit_home)),
        endpoint=endpoint,
        cluster_secret=CLUSTER_SECRET,
        ttl_s=60,
    )
    path.write_bytes(issued.envelope)
    return SealedBundle(path, issued.transport_key)


# meiva:note
# The machine API token the ledger's active generation gives runners: what a unit holding its
# capability presents to the gateway.
def runner_api_token(gateway: Path) -> str:
    return read_secret(gateway, active_generation(gateway)).api.runner


# meiva:note
# What a gateway serves a unit at `/api/bootstrap`: the database endpoint the bundle must name
# and a Redis URL on the same box.
def bootstrap_reply(endpoint: str) -> Reply:
    return Reply(body={"MEIVA_DB_URL": endpoint, "MEIVA_REDIS_URL": "redis://127.0.0.1:6379/0"})


# meiva:note
# A home `meiva init` left as a runner of `gateway_url` (or as a gateway, with `roles`).
def initialized_runner(
    root: Path, *, gateway_url: str, roles: frozenset[str] = frozenset({"agent-runner"})
) -> Path:
    home = (root / "runner").resolve()
    checkout = root / "checkout"
    checkout.mkdir(exist_ok=True)
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            home,
            checkout,
            roles,
            {
                "MEIVA_MACHINE_NAME": MACHINE,
                "MEIVA_GATEWAY_URL": gateway_url,
                "MEIVA_SERVICE_PATH": str(root / "tools"),
            },
            occupied_ports=frozenset(),
        )
    )
    return home
