---
type: doc
title: Agent Cross-Machine Transfer
description: CLI-driven cross-machine workspace and ownership transfer for quiescent native agents.
tags:
- gateway
- agents
- lifecycle
---

# Agent Cross-Machine Transfer

`POST /api/agents/{id}/transfer` (`gateway/agents/transfer/router.py`), exposed exclusively through `meiva agents transfer <id> --to <machine>` (`cli/commands/agents/control.py`), relocates a quiescent native agent (`harness = 'meiva'`) and its runner-local workspace directory from its current `from_machine` to another live `agent-runner` machine in the same cluster.

No SDK surface (`meiva.self` or `meiva.agents`), web UI action, or new `agents_meta.status` / `inbound_messages.kind` enum value is introduced. An agent that wants to relocate itself asks a peer or operator to run `meiva agents transfer` after terminating itself with `meiva.self.terminate()`.

## Preconditions and fast refusal

`gateway/agents/transfer/transfer_plan.py:evaluate_precheck` refuses the request with HTTP 409 before touching either runner when:

- `agents_meta.status == 'running'` (running turns are never preempted or drained by transfer; only `idling` and `terminated` native agents are eligible).
- `agents_meta.harness != 'meiva'` (external-harness sessions own host-local state outside Meiva's runner workspace).
- `from_machine == to_machine`, `to_machine` is not registered as an active `agent-runner`, or `to_machine` is paused or stopped.
- Any in-flight lifecycle command (`pending_lifecycle`) or upload delivery batch (`pending_upload`) exists.
- Any open impersonation lease (`status IN ('requested', 'accepted', 'active')`) or unsettled impersonation handoff (`delta_version > applied_version` or `handoff_applied_at IS NULL`) exists for the agent, since pending handoff files are written on the previous home runner.

Unknown agents or target machines without a live `agent-runner` capability return HTTP 404. Unreachable or stopped source machines return HTTP 503.

## Three-step transfer sequence

Runners do not dial each other directly; the gateway orchestrates a clean three-step sequence over `agent-ops` and lifecycle RPCs:

1. **Step 1 — Terminate on `from_machine` (when `status == 'idling'`)**:
   Under `SELECT status, machine FROM agents_meta FOR UPDATE`, the gateway verifies the agent has not admitted a turn or moved, then dispatches `POST /api/agents/{id}/terminate` (`force=True, kill_all_shell_sessions=True`) to `from_machine`. This sets `status = 'terminated'` with a `terminate` command fence that blocks automatic resurrection during workspace movement, releases source browser pages, closes open UI pages, and kills persistent shell sessions on `from_machine`.
2. **Step 2 — Hub-and-spoke workspace transfer & atomic Postgres cutover while `terminated`**:
   - **Target prepare** (`workspace_transfer_prepare` on `to_machine`): verifies free disk capacity under `<to_home>/workspaces`.
   - **Fenced source export** (`POST /workspace/export` on `from_machine`, `ops/agent_transfer/operations.py:export_workspace_bundle`): verifies `status == 'terminated'` in Postgres before and after killing any remaining shells and deterministically packing `<from_home>/workspaces/<id>` (`ops/agent_transfer/archive_io.py:pack_workspace`). Regular files, directories, and relative symlinks are preserved with SHA-256 digests (`256 MiB` / `20,000` entry ceiling; HTTP 413 on overflow). The gateway holds the framed `MEIVA_WS_V1` bundle in a lifespan-scoped `TransferBundleStore` (`gateway/agents/transfer/bundle_store.py`) under an unguessable token.
   - **Target import and verify** (`workspace_transfer_import` on `to_machine`): pulls `GET /api/internal/agent-transfers/{transfer_id}/bundle`, extracts safely without `extractall`, rescans disk against the manifest (`ops/agent_transfer/bundle_plan.py:verify_disk_snapshot`), backs up pre-existing target directories to `.pre-transfer-<transfer_id>`, and atomically renames the verified tree into `<to_home>/workspaces/<id>`.
   - **Atomic Postgres cutover** (`gateway/agents/transfer/router.py:commit_cutover_blocking`): locks `agents_meta FOR UPDATE`, verifies CAS placement and `status == 'terminated'`, updates `machine = to_machine` while clearing runtime ownership, closes remaining `agent_pages`, deletes stale `agent_shell_ttls`, and records the `"transfer"` audit event (`base/events/declarations/audit.py`). If the CAS check or any pre-cutover step fails, `workspace_transfer_abort` rolls back target staging and `<from_home>/workspaces/<id>` remains intact.
3. **Step 3 — Insert message & wake (`resurrect-explicit`) on `to_machine`**:
   Once cutover commits and `<to_home>/workspaces/<id>` is active, the gateway composes a transfer completion wake message (`gateway/agents/transfer/transfer_plan.py:compose_transfer_wake_message`) describing the completed transfer, active workspace, closed shells/pages, and caller message. The gateway dispatches `POST /api/agents/{id}/resurrect-explicit` to `to_machine` with `ResurrectAgentRequest(resurrected_by=source, prompt=wake_message)`, resurrecting the agent into `'idling'` with both the `'resurrect'` lifecycle marker and the `'chat'` completion prompt queued on `to_machine`. Source and target workspace cleanups finalize best-effort (`workspace_transfer_finalize`).
