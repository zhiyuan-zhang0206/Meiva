"""Tools exposed by external MCP servers. Call `meiva.mcps.<server>.<tool>(...)`
with the tool's named arguments."""

# meiva:note
# Package door. This `__init__` IS the agent-facing `meiva.mcps` namespace, its docstring above is what `meiva.help()` shows, so the package notes live in this comment. It is the facade: it reads the bound context and hands plain values to the `_`-private submodules behind it (see meiva/mcps/docs/structure.meiva.okf.md). `_tools` holds the proxies and the call funnel; `_clients` and `_remote` are the client of the per-machine MCP daemon socket; `_oauth` is the OAuth 2.1 client and token storage for remote (`url`) servers; `_daemon` is the shared per-machine daemon process (`python -m meiva.mcps._daemon`, ops roster session "mcp-daemon") with `_policy`, `_plan`, `_startup` and `_wire` as its strict decision modules and `_guards` as its socket and process effects; `_browser` and `_computer` are the daemon's in-process line clients for the browser-mcp and computer-mcp services. Every submodule is private on purpose: `meiva.mcps.<name>` resolves to an MCP server proxy, so a public submodule would shadow the server of the same name. For the same reason the server configuration (`meiva.mcps.config`, imported by cli/ and ops/) stays outside this package.
# meiva:link meiva/mcps/docs/structure.md

from __future__ import annotations

import sys
import types

from base.agents.lifecycle import (
    release_agent_chrome_pages as release_agent_chrome_pages,
)
from meiva.mcps import _tools
from meiva.mcps._clients import McpClients
from meiva.mcps.config import (
    MCPCallError as MCPCallError,
)
from meiva.mcps.config import (
    MCPConnectError as MCPConnectError,
)
from meiva.mcps.config import (
    MCPError as MCPError,
)
from meiva.mcps.config import (
    MCPToolNotFound as MCPToolNotFound,
)
from meiva.mcps.config import (
    ToolInfo as ToolInfo,
)
from meiva.mcps.config import (
    load_mcp_config,
)
from meiva.sdk_surface import process_context

# The module whose `call_raw` every tool call funnels through, text form and `raw` form alike.
# The SDK metering wrapper replaces that attribute (`meiva/sdk_surface/metering.py`), so it reaches
# the module through this name instead of importing the private `_tools`.
call_funnel = _tools


# meiva:note
# What this process's tools run against, read from the bound context once: its daemon client (dialed on the first request; the SDK never starts servers), the request timeout that client holds, the disk cache and the context's agent identity. A proxy keeps the source it was created with.
def _source() -> _tools.ToolSource:
    context = process_context.current_context()
    clients = context.clients.get(McpClients)
    return _tools.ToolSource(
        remote=clients,
        timeout_seconds=clients.timeout_seconds,
        cache=_tools.DiskCache(context.require_meiva_home() / "mcp_cache"),
        identity=context.identity,
        now=context.require_clock().now().timestamp(),
    )


def load_config() -> dict[str, dict[str, object]]:
    """Merged MCP server map (machine `$MEIVA_HOME/mcp.json` + plugin-bundled `.mcp.json`).

    Returns an empty dict when nothing is configured.

    Raises:
        MCPError: a source file fails to parse / has a non-dict `mcpServers`.
    """
    return load_mcp_config(home=process_context.current_context().require_meiva_home())


def _server_names() -> list[str]:
    return sorted(load_config())


# meiva:note
# The one-line `description` a server's configuration declares: None when it declares none, a `TypeError` naming the server when it declares a non-string. A bad configuration fails the render of `meiva.help(meiva.mcps)` instead of being listed bare.
def _declared_description(server: str, entry: dict[str, object]) -> str | None:
    description = entry.get("description")
    if description is None:
        return None
    if not isinstance(description, str):
        raise TypeError(f"{server}: description must be a string, got {type(description).__name__}")
    return description


def __getattr__(name: str) -> _tools.ServerProxy:
    """`meiva.mcps.<server>` lazily returns server proxy (module-level PEP 562)."""
    if name.startswith("_"):
        raise AttributeError(name)
    config = load_config()
    if name not in config:
        raise AttributeError(
            f"meiva.mcps has no server {name!r} (not configured in `$MEIVA_HOME/mcp.json`; have: {sorted(config)})"
        )
    description = _declared_description(name, config[name])
    return _tools.ServerProxy(name, _source(), description)


def __dir__() -> list[str]:
    """List every configured server name."""
    return _server_names()


class _McpsModule(types.ModuleType):
    """The class of the `meiva.mcps` module object itself.

    Carries `__all_for_meiva__` as a property, like `meiva.skills`: the agent-visible members are
    every configured server (`meiva.mcps.<server>`, served by the module `__getattr__`), and the servers come from `$MEIVA_HOME/mcp.json` and the plugins'
    bundled `.mcp.json`, so the list is live rather than a literal. Only this list decides what
    `help()` and the metering wrappers see; a new public name or submodule here widens nothing.
    """

    @property
    def __all_for_meiva__(self) -> list[str]:
        """The configured server names, sorted. A process that holds no SDK context (a host
        building an installation) has no home to read servers from and lists none."""
        return _server_names() if process_context.ambient_context() is not None else []


sys.modules[__name__].__class__ = _McpsModule
