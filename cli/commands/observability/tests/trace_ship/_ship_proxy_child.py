# meiva:note
# Child entry for the proxy test: one POST to `ENDPOINT` under the proxy environment the parent
# gives this process. The proxy variables are read when an httpx client is built, so a test
# controls them by running the client in a process that has exactly the environment under test.
#
# usage: _ship_proxy_child.py ship TRACES_DIR ENDPOINT
#        _ship_proxy_child.py control ENDPOINT
#
# `ship` replays `TRACES_DIR/spans.jsonl` to `ENDPOINT` the way `meiva trace ship` does. `control`
# POSTs with a default httpx client and prints the status it got, which shows the environment
# does route a default client through the proxy.

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from cli.commands.observability import trace
from cli.commands.observability.plans.trace_ship.files import ship_window
from cli.commands.observability.plans.trace_ship.target import ShipTarget


def main() -> int:
    mode = sys.argv[1]
    if mode == "ship":
        traces_dir = Path(sys.argv[2])
        trace.ship_files(
            [traces_dir / "spans.jsonl"],
            {},
            target=ShipTarget(endpoint=sys.argv[3], headers={}, label="tempo"),
            window=ship_window(None, None),
            traces_dir=traces_dir,
        )
    elif mode == "control":
        with httpx.Client(timeout=30.0) as client:
            print(client.post(sys.argv[2], content=b"probe").status_code)
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
