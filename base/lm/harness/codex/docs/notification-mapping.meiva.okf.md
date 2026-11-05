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
| `item/completed` (`userMessage`) | confirms foreground steering when the thread, turn and client message IDs match a pending request |
| `item/started` / `item/completed` (`commandExecution`) | `ToolUse` / `ToolResult` (`is_error` when the status is failed/declined or the exit code is non-zero) |
| `item/started` / `item/completed` (`fileChange`) | `ToolUse` / `ToolResult` named `patch` |
| `item/*` (`mcpToolCall`, `dynamicToolCall`, `webSearch`) | `ToolUse` / `ToolResult` |
| `item/started` / `item/completed` (`collabAgentToolCall`) | `ToolUse` / `ToolResult`, keyed by item ID with the operation and receiver status |
| `item/reasoning/summaryTextDelta`, `item/reasoning/textDelta` | `ReasoningDelta` |
| `item/completed` (`reasoning`) | `ReasoningText` when text is available |
| `thread/tokenUsage/updated` | per-turn `Usage`, accumulated by `turns.TurnRecord` into `SessionTotals` |
| `turn/started` during launch | provisional interrupt target |
| `turn/completed` naming our turn | `TurnCompleted` (`status` → `subtype`, `interrupted` from the provider status) |
| `turn/completed` naming any other turn | `UnsolicitedOutput(source="turn/completed")` carrying that turn's reply |
| `error` with `willRetry=false`, naming our turn | `TurnFaulted` |
| `error` with `willRetry=false`, naming any other turn | `UnsolicitedOutput(source="error")` |

Plan, image, standalone sub-agent lifecycle, queue and rate-limit notifications have no timeline activity mapping.

The authoritative `turn/start` response emits `ForegroundAccepted` before
buffered notifications are dispatched.

Parent: [[base/lm/harness/codex/docs/codex.meiva.okf.md|the Codex app-server session bridge]].
