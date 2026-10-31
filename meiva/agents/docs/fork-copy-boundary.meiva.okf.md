---
type: doc
title: Agent fork copy boundary
description: What meiva.agents.spawn(fork_from=...) copies from the parent conversation checkpoint and how in-flight tool calls settle in the forked child.
tags: [agent-view, sdk, agent-lifecycle]
---

# Agent fork copy boundary

A fork (`meiva.agents.spawn(..., fork_from=parent_id, idempotency_key=...)`) copies the source
agent's newest saved conversation state as of the call, not its live turn. When the source is
between a tool call and its result, the fork inherits the call without the result, and the new
agent's next turn settles that pair as interrupted instead of re-running anything. The interrupted
call is therefore not a task to finish: its effects may already exist, so the new agent inspects
state before repeating it.

The copy is the whole conversation, so the child carries that history and pays its size on every
call; the parent's own history is unchanged.

Parent: [[meiva/agents/docs/agents.meiva.okf.md]].
