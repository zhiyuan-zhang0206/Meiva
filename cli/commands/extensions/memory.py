from __future__ import annotations

import sys

import httpx

from base.cluster.machine import (
    GatewayAccess,
    MachineIdentityReader,
    gateway_api_base,
    gateway_auth_headers,
)
from base.deploy.git.memory_repo import MemoryBranchMismatch
from base.host.net.http_dial import post as dial_post
from cli.commands.extensions._plugin_scaffold import (
    ScaffoldResult as ScaffoldResult,
)
from cli.commands.extensions._plugin_scaffold import (
    run_plugin_scaffolds as run_plugin_scaffolds,
)
from cli.commands.extensions.plans.memory_search import render_results, search_body

_TIMEOUT_S = 30.0


# meiva:note
# Explicitly initialize enabled plugin-owned memory resources.
#
# Memory repository branch validation belongs to this operator-requested provisioning path,
# never to converge or service startup.
def cmd_memory_init(*, machine: MachineIdentityReader) -> int:
    print("initializing memory resources...")
    try:
        result = run_plugin_scaffolds(machine=machine)
    except MemoryBranchMismatch as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 1
    if result.ran:
        print(f"scaffolded: {', '.join(result.ran)}")
    else:
        print("no enabled plugin scaffolds")
    return 0


def _refresh_index(access: GatewayAccess) -> None:
    url = f"{gateway_api_base(access.gateway_url)}/api/memory/refresh"
    try:
        resp = dial_post(url, timeout=_TIMEOUT_S, headers=gateway_auth_headers(access))
        resp.raise_for_status()
        data = resp.json()
        print(f"  index refreshed: {data.get('status', 'ok')}")
    except httpx.HTTPError as exc:
        print(f"  ✗ refresh failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def cmd_memory_refresh(*, access: GatewayAccess) -> int:
    print("refreshing memory index...")
    _refresh_index(access)
    return 0


# meiva:note
# Search gateway memory while keeping paths relative to its memory root.
#
# The request body and the rendering of the answer are `plans.memory_search`; this shell
# dials the gateway with the caller's `access`, echoes the gateway's error text on a refusal
# and prints the rendering. A refusal still raises `httpx.HTTPStatusError` after its text.
def cmd_memory_search(query: str, *, limit: int, json_output: bool, access: GatewayAccess) -> int:
    url = f"{gateway_api_base(access.gateway_url)}/api/memory/search"
    resp = dial_post(
        url,
        json=search_body(query, limit),
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(access),
    )
    if resp.status_code >= 400:
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()
    print(render_results(resp.json(), json_output=json_output))
    return 0
