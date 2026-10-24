---
type: doc
title: MCP Facade and Daemon Structure
description: How meiva.mcps separates its agent-facing facade from an implementation that takes values, and how the MCP daemon splits into a thin shell and strict pure decision modules.
tags:
- core
- mcp
---

# MCP Facade and Daemon Structure

**Facade and implementation**: `meiva/mcps/__init__.py` is the facade. For each server proxy it reads the bound `MeivaContext` once and builds a `meiva/mcps/_tools.py:ToolSource` of plain values: the context's `McpClients` (it dials the daemon socket that `meiva.boot.process_clients` derived from the home, on its first request, and implements the two daemon calls), the request timeout that client holds, the disk cache under the home (`meiva/mcps/_tools.py:DiskCache`, a directory) and the context's `AgentIdentity`. Everything in `meiva/mcps/_tools.py` and `meiva/mcps/_remote.py` takes those values. The agent id rides on every daemon request as an explicit argument resolved by `meiva/mcps/_tools.py:resolve_agent_id`: a borrowed lease is revalidated at each use and takes precedence (the precedence is the attribution plan's `meiva/sdk_surface/pure/caller/identity_plan.py:attributed_agent_id`, shared with `meiva.sdk_surface.agent_identity.agent_id`), and an invalid lease raises instead of falling back. A proxy keeps its source. The cached client owns its socket, not a settings snapshot: dial and response waiting use that request's timeout budget.

**Daemon as values and shells**: `meiva/mcps/_daemon.py` is the shell; what it decides is a value or a strict pure module. `meiva/mcps/_policy.py` holds the frozen `DaemonConfig` that `main` reads from the process once (a setting changed while the daemon runs takes effect at the next daemon start) and the retry delay of a failed try. `meiva/mcps/_plan.py` says which transport reaches a server from its entry and why a host cannot run it. `meiva/mcps/_startup.py` says which socket a launch binds, whether a live occupant forbids starting, and which processes are ghost daemons of this unit. `meiva/mcps/_wire.py` builds the JSON-line responses. `meiva/mcps/_guards.py` holds the effects behind the socket ownership guards (probe, kill, unlink). Tests run real MCP servers (a stdio fixture child, an in-loop streamable-http server on port 0), real socket pairs and real processes, and patch nothing of the module.

## Related
- [[meiva/mcps/docs/mcps.meiva.okf.md|MCP Server System]] — what the SDK exposes and where servers come from
- [[agent/docs/mcp-daemon.meiva.okf.md|MCP Daemon]] — the daemon's responsibilities and entry points
