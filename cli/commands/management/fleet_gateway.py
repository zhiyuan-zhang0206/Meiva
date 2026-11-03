# meiva:note
# Fleet update roster query, shipped to the NEW gateway over stdin. Keep this program in one
# source file: the updater sends this exact text, and its regression tests execute the same module
# against current gateway APIs.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import json
import sys

from pydantic import JsonValue

from base.cluster.machine import gateway_api_base, gateway_auth_headers
from base.cluster.machine.boot import gateway_access_from_boot
from base.config import ConfigBoot
from base.host.net.http_dial import get
from cli.boundary.json_body import JsonShapeError, response_list


def _field(row: JsonValue, key: str) -> JsonValue:
    if not isinstance(row, dict):
        raise JsonShapeError(f"a roster row is {type(row).__name__}, not an object")
    return row[key]


def main(argv: list[str]) -> int:
    if argv != ["roster"]:
        print(f"usage: fleet_gateway.py roster (got {argv!r})", file=sys.stderr)
        return 2
    access = gateway_access_from_boot(ConfigBoot())
    base, headers = gateway_api_base(access.gateway_url), gateway_auth_headers(access)
    rows = get(f"{base}/api/cluster/roster?fresh=true", headers=headers, timeout=90)
    rows.raise_for_status()
    keep = (
        "name",
        "online",
        "identity_mismatch",
        "head_sha",
        "running_sha",
        "serve_agent_runner",
    )
    roster = response_list(rows)
    print(json.dumps([{key: _field(row, key) for key in keep} for row in roster]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
