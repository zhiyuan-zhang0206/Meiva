# meiva:note
# The child process of the OAuth connect tests in `test_connect.py`. Building an OAuth client
# installs the daemon's fixes into the MCP SDK for the rest of the process, so the connect runs in a
# process of its own. It connects to the server named in argv, as a daemon would (the home comes
# from the environment), lists its tools and prints what came of it as one JSON object: the tool
# names and flow locks of a connection, or the class of the error that stopped it.
# Arguments: the repository root, the server name, the connect timeout in seconds ("-" keeps the
# one the configuration names) and, optionally, the home the daemon configuration names (default:
# the environment's home).

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path


async def main() -> None:
    root, server, timeout_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    config_home = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    sys.path.insert(0, root)
    import meiva.mcps._daemon as daemon_mod
    from base.paths import meiva_home
    from meiva.mcps.tests.daemon.daemon_support import daemon_config

    locks: dict[str, asyncio.Lock] = {}
    home = config_home if config_home is not None else Path(meiva_home())
    config = daemon_config(home)
    if timeout_arg != "-":
        config = replace(config, connect_timeout_seconds=float(timeout_arg))
    try:
        session, stack = await daemon_mod.connect_server(server, config, locks)
    except Exception as error:
        print(json.dumps({"outcome": "failed", "error": type(error).__name__}))
        return
    tools = await session.list_tools()
    await stack.aclose()
    print(
        json.dumps(
            {"outcome": "connected", "tools": [t.name for t in tools.tools], "locks": list(locks)}
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
