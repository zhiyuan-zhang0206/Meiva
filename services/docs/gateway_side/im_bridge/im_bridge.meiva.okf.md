---
type: doc
title: IM Bridge — the product frontend
description: "The IM Bridge (`services/entrypoints/im_bridge/`) is the product frontend: every user-facing IM channel (Weixin) is an adapter the bridge owns — message envelope, command routing, per-chat switch state, SSE subscription push of agent dialog."
tags: []
---

# IM Bridge — the product frontend

## What it is
The optional IM Bridge daemon runs the configured Weixin adapter and retains
shared command routing, durable timeline delivery and manual notice lookup.
Telegram and Feishu are retired. With the remaining adapter disabled, its health
server and background tasks stay alive until shutdown; the web UI and notice
queue do not require an enabled IM adapter.

**Role affiliation**: gateway side. `supervision/roster/__init__.py` declares `GATEWAY` capabilities and `requires_db=True`: notice polling reads `agent_notices` directly. The root supervisor monitors the roster's `/healthz` identity probe.

## Core Responsibilities
- **Adapters** (`services/entrypoints/im_bridge/adapters/<channel>.py`): each channel is one adapter sharing the core — envelope, command routing, per-channel session state. Unset credentials keep that channel inactive; invalid imports or configuration fail startup. `MEIVA_IM_DISABLED_ADAPTERS` disables channels. WeChat's one-off interactive QR login (`python -m services.entrypoints.im_bridge.adapters.weixin_login --login`) lives in `adapters/weixin_login.py`.
- **Dialog push**: SSE only wakes a committed `GET /timeline` read; periodic pulls cover pre-commit events with no later SSE. The dialog filter selects user-originated `inbound_chat` and `agent_chat`. Qualified persistent source/block identities are accepted into the shared IM Outbox in the same transaction as the per-chat watermark. Timestamp ordering survives compact renumbering; numeric item positions break ties. Unqualified legacy sources hold acceptance instead of resetting the cursor. Explicit switch invocations retain immutable replay batch receipts, including empty selections. The worker sends frozen adapter manifests and retains ambiguous attempts without automatic replay; see [[services/docs/gateway_side/im-outbound-retry/im-outbound-retry.meiva.okf.md]].
- **Notices**: `/notice list` returns open notice text to the requesting conversation. UI notices and their response-required semantics remain owned by the shared notice queue. Automatic Telegram notice forwarding is retired.
- **Command set**: `/list` / `/switch` / `/status` / `/spawn` / `/commands` / `/help`, plus `/notice` (notice queue) — see `core.py`; initialized selection is canonical in `im_bridge_cursors`; `state.py` keeps its derived JSON cache.
- **Spawn menu** (`spawn_menu.py`): interactive agent-spawn flow from the chat (`SpawnDraft` lives in `types.py`; `SpawnMenuMixin` owns the flow). Creation event keys and draft recovery limits: [[im-spawn-menu.meiva.okf.md]].
- **Push watchdog** (`push_watchdog.py`): context-token failure alerts and recovery hints. Only adapter-proven unstarted sends receive a bounded retry; see [[services/docs/gateway_side/im-outbound-retry/im-outbound-retry.meiva.okf.md]].
- **Push health sampling** (`outbound/health.py`): one structured `im_push_health` event per configured channel (`MEIVA_IM_PUSH_HEALTH_CHANNELS`) each `MEIVA_IM_PUSH_HEALTH_INTERVAL_SECONDS` (default 60) — seconds since the channel's newest delivered push (`last_ok_age_s`; the negative `PUSH_HEALTH_NEVER_SENT` sentinel when none is on record) and pushes still waiting for a delivery attempt (`pending`, the durable outbox's `queued`+`sending` rows). Detection only: the staleness alert rule lives in Grafana (task #4935).
- **Configuration** (`config.py`, `daemon.py`): the bridge reads no `settings`. `daemon.py`, its composition root, reads the process's `ConfigBoot` and builds the frozen `ImBridgeConfig` slice (core, gateway client, push watchdog, notice bridge) — plus the gateway client from the gateway URL and the machine API token's Bearer (`gateway_auth_headers(access)`; it never logs in or holds the cluster secret), and passes them through constructors, with a `Clock` and a `random.SystemRandom` (ids, jitter, uin): nothing below reads the clock or randomness. Its disk state is one `state_dir` value; nothing below the roots reads the home or the environment. `/send` accepts the write generation's machine API tokens (`accepted_digests`). Strict plans: `adapters/run_plan.py`, `outbound/push_plan.py`, `outbound/claim_fence.py`.
- **Ops-alert fan-out**: `IMBridgeCore.notify_user` sends ops alerts (P0/P1 by default) to the owner chat — see [[gateway/alerts/docs/alerts.meiva.okf.md]]. Each channel retries only a proven unstarted send; uncertain or partial failures stop; a failed channel still reports `error: <TypeName>` (all-failed → `/send` 502 → the caller's `notified_at` re-send gate).


## Key Dependencies
- [[gateway/routers/docs/routers.meiva.okf.md]] — the gateway REST client + SSE subscription the bridge consumes
- [[gateway/alerts/docs/alerts.meiva.okf.md]] — IM notification fan-out for ops alerts

## Entry Points
- `services/entrypoints/im_bridge/daemon.py` — `.venv/bin/python -m services.entrypoints.im_bridge.daemon`
- `services/entrypoints/im_bridge/core.py` — shared envelope / command routing / SSE push (`IMBridgeCore`)
- `services/entrypoints/im_bridge/gateway_client.py` — gateway REST and SSE transport (`GatewayClient`)
- `services/entrypoints/im_bridge/state.py` — persisted per-chat switch state
- `services/entrypoints/im_bridge/cursor_store.py` — compatibility push positions
- `services/entrypoints/im_bridge/outbound/{store,health,worker,types}.py` — atomic timeline acceptance and shared delivery lifecycle (also the periodic push-health sample)
- `services/entrypoints/im_bridge/types.py` — shared message, chat-state, and adapter contracts
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`supervision/roster/healthz.py`)

## Notes
- Bridge ↔ agent dialog is human messages in, agent text out — tool execution / reasoning / other agents' messages are never pushed to IM

## Weixin inbound identity

Weixin sources commit native receipts and frozen routes before cursor checkpoint.
A new account binding drains unproven backlog into quarantine, then activates on
a committed real empty response. Unproven commands remain uncertain.
See [[weixin-inbound-identity]].


## Task ownership and failures

See [[im-bridge-lifecycle]] for service-owned tasks, explicit retry boundaries
and strict local state restoration.

Notices require a response: users reply or dismiss. Ordinary information stays in chat.
