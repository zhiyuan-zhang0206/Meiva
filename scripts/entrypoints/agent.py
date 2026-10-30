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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from base.cluster.machine.boot import gateway_access_from_boot
from base.config import ConfigBoot
from base.host.net.http_dial import post as dial_post
from scripts.entrypoints.spawn_request import build_spawn_request, parse_spawn_agent_id


def main() -> None:
    access = gateway_access_from_boot(ConfigBoot())
    request = build_spawn_request(access, idempotency_key=uuid.uuid4().hex)
    resp = dial_post(request.url, json=request.json, headers=request.headers)
    resp.raise_for_status()
    agent_id = parse_spawn_agent_id(resp.json())
    print(f"agent_id={agent_id}")


if __name__ == "__main__":
    main()
