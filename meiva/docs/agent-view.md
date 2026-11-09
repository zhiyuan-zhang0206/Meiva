# Agent LLM View — What the Agent Sees from the LLM Perspective

## What it is

This is the agent's "view" at runtime — from the LLM's perspective, what the agent can perceive and what it can do. Parallel to agent-runtime (the implementation mechanism at runtime), but focused on the agent's subjective experience rather than implementation details.

## Sub-concepts

### SDK Surface
- [SDK Surface — meiva.* Tool Overview (Index)](sdk-surface/sdk-surface.md) — **index**, pointing to the following modules
- [meiva.files — File System](files.md) — file system operations
- [meiva.shell — Shell Operations](../shell/docs/shell.md) — shell commands and persistent sessions
- [meiva.agents — Agent Interop](../agents/docs/agents.md) — agent interop
- [Tasks](../../meiva_builtins/plugins/meiva_fleet/docs/tasks/tasks.md) — task registry `meiva.tasks` (injected by meiva_fleet plugin)
- [MCP Server System](../mcps/docs/mcps.md) — MCP tool servers
- [meiva.web — Web Access](../web/docs/web.md) — network access
- [meiva.understand — Multimodal Understanding Primitive](../understand/README.md) — multimodal understanding primitives
- [watcher](../watcher/docs/watcher.md) — background watchers
- [self](../self/README.md) — agent self
- [meiva.ui — User Interface](ui.md) — user interface
- [meiva.memory — Long-term Memory Pool (Plugin Provided)](../../meiva_builtins/plugins/meiva_memory/docs/memory-api.md) — long-term memory pool
- [Skill System](../skills/README.md) — skill registry

### System Prompt & Messages
- [System Prompt — System Prompt](../../agent/graph/docs/system-prompt/system-prompt.md) — how the system prompt is built
- [Message Format](../../agent/messages/docs/message-format.md) — message formats sent and received by agents

### Execution Environment
- [Context Window — Context Management](../../agent/graph/docs/context-window/context-window.md) — context management + compaction
- [Tool Execution — Fault-Isolated Code Execution](../../agent/graph/exec/docs/exec.md) — code execution sandbox

## Relationship to Other Domains
- [Agent Runtime](../../agent/docs/agent-runtime/agent-runtime.md) — runtime implementation (parallel perspective)
- [Cross-Cutting Concerns](../../agent/docs/cross-cutting.md) — cross-cutting concerns (environment variables, logging)
- [Extensions](../../meiva_builtins/docs/extensions.md) — extension system (plugin, skill)
