"""dev startup entry — spawn a root agent process and print its id.

Production spawn goes through the SDK (`meiva.agents.spawn(...)`) or the
gateway endpoint; this script is the "first time starting up / no root
agent running" bootstrap entry (chicken-and-egg solution — how do you
start the first agent when the agents_meta table is empty).

    .venv/bin/python scripts/entrypoints/agent.py

Only launches the process; **does not deliver an inbound** — after spawn,
the agent idles waiting for inbound. To make it do work, use the web UI
to send a message to the printed agent_id.

**Gateway must be up first** — the bootstrap also goes through HTTP
`POST /api/keyed/v1/agents`, the same path as the SDK / frontend. Gateway is
centralized at the gateway.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.cluster.machine.ambient import gateway_access_from_settings
from base.host.net.http_dial import post as dial_post


@dataclass(frozen=True)
class SpawnRequest:
    url: str
    json: dict[str, str]
    headers: dict[str, str]


def build_spawn_request(access: GatewayAccess, *, idempotency_key: str) -> SpawnRequest:
    base = gateway_api_base(access.gateway_url)
    # The keyed spawn route is authenticated: present the machine API token (or the
    # cluster secret) as a Bearer token; without it the gateway rejects this spawn with 401.
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
    raw_id = payload["id"]
    if not isinstance(raw_id, int | str):
        raise TypeError(f"spawn response id must be int or str, got {type(raw_id).__name__}")
    return int(raw_id)


def main() -> None:
    access = gateway_access_from_settings()
    request = build_spawn_request(access, idempotency_key=uuid.uuid4().hex)
    resp = dial_post(request.url, json=request.json, headers=request.headers)
    resp.raise_for_status()
    agent_id = parse_spawn_agent_id(resp.json())
    print(f"agent_id={agent_id}")


if __name__ == "__main__":
    main()
