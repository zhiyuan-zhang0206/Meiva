---
type: doc
title: What an app-server notification becomes
description: '`base/lm/harness/codex/` — the notification mapping: each app-server method the bridge reads and the bridge event it produces, and why everything else maps to nothing.'
tags:
- base
- library
- llm-inference
- codex
---

# What an app-server notification becomes

`turns.NotificationKind` is the set of messages the bridge reads, and
`turns.inbound` classifies one by its method. This table is what each kind turns
into.

| app-server method | Bridge event |
|---|---|
| `item/agentMessage/delta` | `TextDelta` |
| `item/completed` (`agentMessage`) | `AssistantText`; the `final_answer` phase also becomes the turn's reply text |
| `item/started` / `item/completed` (`commandExecution`) | `ToolUse` / `ToolResult` (`is_error` when the status is failed/declined or the exit code is non-zero) |
| `item/started` / `item/completed` (`fileChange`) | `ToolUse` / `ToolResult` named `patch` |
| `item/*` (`mcpToolCall`, `dynamicToolCall`, `webSearch`) | `ToolUse` / `ToolResult` |
| `thread/tokenUsage/updated` | per-turn `Usage`, accumulated by `turns.TurnRecord` into `SessionTotals` |
| `turn/started` | binds the provider's name for the turn this session is starting (`TurnTicket.provider_turn_id`); any other moment, nothing |
| `turn/completed` naming our turn | `TurnCompleted` (`status` → `subtype`, `interrupted` from the interrupt flag or status) |
| `turn/completed` naming any other turn | `UnsolicitedOutput(source="turn/completed")` carrying that turn's reply |
| `error` with `willRetry=false`, naming our turn | `TurnFaulted` |
| `error` with `willRetry=false`, naming any other turn | `UnsolicitedOutput(source="error")` |

Everything else (reasoning, plan, image, sub-agent, queue and rate-limit
events) maps to nothing: the runtime does not model it.

Parent: [[base/lm/harness/codex/docs/codex.meiva.okf.md|the Codex app-server session bridge]].
