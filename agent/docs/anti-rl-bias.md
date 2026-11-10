# Anti-RL-Bias Mechanisms

RLHF-tuned models carry two habits that hurt a long-lived agent: treating a
text turn as "I answered, therefore I'm done", and going quiet when unsure. Meiva
answers both from the system layer — first-class verbs, daemons and hooks that
turn ambiguous silence into supervisable state. A fleet that runs unattended
cannot afford agents that look done when they are not, or that stall silently
instead of asking; an agent that declares intent beats a prompt that asks it to.

A completed model response without a tool call may end quietly, including
a reasoning-only response. Silence alone does not authorize another model
call or a forced completion message. Explicit wake-ups and task reminders
continue to supervise outstanding work.

## Mechanisms

1. **Idle heartbeat** — `services/wake/heartbeat/` ([Heartbeat — Idle Agent Heartbeat](../../services/docs/gateway_side/heartbeat.md)).
   A gateway daemon polls idle agents and sends a nudge naming three honest
   options: still working (do nothing), waiting (`meiva.self.pause_heartbeat(duration)`
   suppresses nudges for a declared window; real wake-ups still arrive), or done
   (terminate). Opt-out is an active agent choice, not an escalation chain
   ([why](../../docs/decisions/runtime/processes/health/2026-06-22-heartbeat-opt-out-over-escalation.md)).
2. **Capabilities drift check** — `agent/graph/hooks/capabilities.py`. Before every LLM
   call the live skill index is diffed against the snapshot the context window
   was built from; skills installed since are named in one system note, so an
   agent cannot silently rebuild a capability it already has.
3. **Task progress reminders** — the gateway tasks API (`remind_interval_seconds`).
   The owner of an in-progress task is periodically reminded to post progress;
   reminders cannot be disabled (capped at 24 h), so a task that goes dark is a
   visible fact.
4. **Delivery semantics** — a bare text turn delivers nothing:
   `meiva.agents.send_message` (peers) and a reply that carries the user-display prefix (the user) are the only
   delivery verbs, so "not done yet" must be said out loud and silence reads as
   busy ([why](../../docs/decisions/agents/messages/2026-08-01-one-send-is-one-inbound.md)).
5. **Standing memory injection** — the memory index (`MEMORY.md`) is injected at
   cold start and after every compact, so long-lived rules stay in front of the
   agent ([Memory Injection — The Three Memory Notes Behind the System Prompt](../graph/docs/context-notes/memory-injection.md)).
6. **Prompt-layer discipline** — agents give scope and trade-offs, never time
   estimates, and describe current behavior
   ([`communicating-with-user.md`](../../docs/conventions/communicating-with-user.md)).
