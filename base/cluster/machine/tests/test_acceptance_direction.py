# meiva:note
# The admission decision is a value decision: nothing in the strict door reads the authority.
#
# `accepted_digests` decides from the digests its caller read; the read itself is
# `base.host.db_authority.admission.admission_inputs`, called by the daemons that own an admission.
# These tests pin that direction, so moving the read back into the strict module — the shape that
# made `base/cluster/machine/__init__.py` depend on two impure modules — fails here as well as in
# `meizel check`.
# meiva:link base/cluster/machine/docs/machine.meiva.okf.md

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from base.cluster import machine
from base.cluster.machine import GatewayAccess, accepted_digests
from base.host.db_authority.digests import token_digest
from base.host.net.auth import ClusterSecretMissingError

_MODULE = Path(machine.__file__).resolve()
# meiva:note
# The two modules that read a store: the unit capability under a home, and the generation ledger.
# `digests.py` is the pure SHA-256 owner and stays importable from a strict module.
_AUTHORITY_READERS = {
    "base.host.db_authority.api",
    "base.host.db_authority.unit",
}
_HUMAN = "human-secret-do-not-use"


def _access(**overrides: object) -> GatewayAccess:
    base = GatewayAccess(
        gateway_url="http://127.0.0.1:8000",
        cluster_secret=_HUMAN,
        remote_plane=False,
        machine_token=None,
        launcher_profile=None,
        gateway_home=False,
    )
    return replace(base, **overrides) if overrides else base


# meiva:note
# The read moved to `admission_inputs`; its return makes the strict module impure again.
def test_the_strict_door_does_not_read_the_authority() -> None:
    imported = {
        node.module
        for node in ast.walk(ast.parse(_MODULE.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    reads = {name for name in imported if name in _AUTHORITY_READERS}
    assert reads == set()
    assert not hasattr(machine, "daemon_acceptance")


def test_the_decision_is_made_from_the_digests_it_is_handed() -> None:
    home = Path("/srv/unit/home")
    unit = frozenset({"unit-gateway-digest", "unit-runner-digest"})
    generation = frozenset({"gateway-digest", "runner-digest"})
    assert accepted_digests(_access(), home=home, unit=unit, generation=generation) == unit
    assert accepted_digests(_access(), home=home, unit=None, generation=generation) == generation
    remote = _access(remote_plane=True)
    assert accepted_digests(remote, home=home, unit=None, generation=generation) == frozenset(
        {token_digest(_HUMAN)}
    )


def test_a_relative_home_is_refused_before_anything_is_read() -> None:
    with pytest.raises(ValueError, match="absolute home"):
        accepted_digests(_access(), home=Path("unit/home"), unit=None, generation=frozenset())


def test_a_gateway_home_without_a_secret_refuses() -> None:
    with pytest.raises(ClusterSecretMissingError):
        accepted_digests(
            _access(cluster_secret=""), home=Path("/srv/gateway"), unit=None, generation=frozenset()
        )
