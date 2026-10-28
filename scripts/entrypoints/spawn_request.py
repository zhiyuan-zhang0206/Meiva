# meiva:note
# The one POST the bootstrap spawn sends: the keyed route, the headers that authenticate it, the
# idempotency key the caller drew, and the id the gateway answers with. Building it reads nothing.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers

__all__ = ["SpawnRequest", "build_spawn_request", "parse_spawn_agent_id"]


@dataclass(frozen=True)
class SpawnRequest:
    # meiva:note
    # One keyed spawn POST: where it goes, what it carries and which headers authenticate it.

    url: str
    json: dict[str, str]
    headers: dict[str, str]


# meiva:note
# The keyed spawn route is authenticated: present the machine API token (or the cluster secret) as
# a Bearer token; without it the gateway rejects this spawn with 401.
def build_spawn_request(access: GatewayAccess, *, idempotency_key: str) -> SpawnRequest:
    base = gateway_api_base(access.gateway_url)
    headers = {
        **gateway_auth_headers(access),
        "Idempotency-Key": idempotency_key,
        SCOPE_HEADER: PRINCIPAL_SCOPE,
    }
    return SpawnRequest(
        url=f"{base}/api/keyed/v1/agents",
        json={"spawner": "user"},
        headers=headers,
    )


def parse_spawn_agent_id(payload: Mapping[str, object]) -> int:
    # meiva:note
    # The spawn response carries the new agent's id as an int or a string; anything else is a
    # protocol error, not a zero.
    raw_id = payload["id"]
    if not isinstance(raw_id, int | str):
        raise TypeError(f"spawn response id must be int or str, got {type(raw_id).__name__}")
    return int(raw_id)
