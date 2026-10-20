---
type: doc
title: MCP endpoint model capability failures
description: Which MCP tools keep working without the model catalog, and the lazy-SDK-types invariant of the disabled endpoint.
tags: [gateway, mcp]
---

# MCP endpoint model capability failures

Registration of all seven tools and MCP authentication do not read the catalog. A tool is as
available as the API route it forwards to: `list_agents` and `get_agent` preserve database facts
and return null vision metadata when it is unavailable, as the HTTP reads do.
`spawn_agent_guarded_v1` still requires the complete catalog; the route's unavailable-catalog
error reaches the caller as a tool error carrying the API envelope. `cluster_status`,
`send_message`, `get_messages` and `terminate_agent` retain their independent requirements and
remain callable.

A disabled endpoint answers 404 without constructing an MCP server or session manager; the enabled
manager exposes the seven control tools. The builders import their concrete SDK return classes
locally, so those type names are unavailable in the module signature namespace and their return
annotations remain omitted. This does not imply that the SDK module itself is unloaded.

## Relationship to Other Nodes

- [[gateway/mcp_server/docs/mcp-endpoint/mcp-endpoint.meiva.okf.md|Gateway /mcp endpoint]] — the node these failure behaviors belong to.
