# meiva:note
# What a daemon's admission is decided from: the unit capability and the active generation.
#
# The decision itself is `base.cluster.machine.accepted_digests`; this module reads the two inputs it
# takes — the digests an installed unit capability admits (None when none is installed) and, when
# there is none, the gateway home's active write generation.
# meiva:link base/cluster/machine/docs/machine.meiva.okf.md

from __future__ import annotations

from pathlib import Path

from base.host.db_authority.api import acceptance
from base.host.db_authority.digests import token_digest
from base.host.db_authority.unit import load_unit_capability

__all__ = ["admission_inputs"]


# meiva:note
# The capability digests (None when no capability is installed) and the generation's.
#
# The generation is read only when no capability decides the answer, so a remote unit's admission
# never depends on a ledger it does not keep.
def admission_inputs(home: Path) -> tuple[frozenset[str] | None, frozenset[str]]:
    capability = load_unit_capability(home)
    if capability is not None:
        api = capability.api
        return frozenset({api.gateway, token_digest(api.token)}), frozenset()
    return None, frozenset(acceptance(home).values())
