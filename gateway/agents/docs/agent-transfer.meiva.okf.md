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
- `from_machine == to_machine` or `to_machine` is paused in `machine_dispatch_pauses`.
- Any open impersonation lease (`status IN ('requested', 'accepted', 'active')`) or unsettled impersonation handoff (`delta_version > applied_version` or `handoff_applied_at IS NULL`) exists for the agent, since pending handoff files are written on the previous home runner.

Unknown agents or target machines without a live `agent-runner` capability return HTTP 404.

## Hub-and-spoke workspace transfer (`MEIVA_WS_V1`)

Runners do not dial each other directly; the gateway orchestrates a four-step hub-and-spoke sequence over `agent-ops`:

1. **Target prepare** (`workspace_transfer_prepare` on `to_machine`): verifies free disk capacity under `<to_home>/workspaces`.
2. **Fenced source export** (`POST /workspace/export` on `from_machine`, `ops/agent_transfer/operations.py:export_workspace_bundle`): verifies the snapshot fence `(machine, status, harness, runtime_owner, runtime_generation, last_admission_at, session_index)` in Postgres before and after killing shell sessions up to `expected_session_index` and deterministically packing `<from_home>/workspaces/<id>` (`ops/agent_transfer/archive_io.py:pack_workspace`). Regular files, directories and relative symlinks within the workspace root are preserved with SHA-256 digests (`256 MiB` / `20,000` entry ceiling; HTTP 413 on overflow); FIFOs, sockets, device nodes and escaping paths fail closed. The gateway holds the framed `MEIVA_WS_V1` bundle in a lifespan-scoped `TransferBundleStore` (`gateway/agents/transfer/bundle_store.py`) under an unguessable token.
3. **Target import and verify** (`workspace_transfer_import` on `to_machine`): pulls `GET /api/internal/agent-transfers/{transfer_id}/bundle`, extracts without `extractall` into a sibling staging directory while rejecting symlink traversal, rescans disk against the manifest (`ops/agent_transfer/bundle_plan.py:verify_disk_snapshot`), backs up any pre-existing target directory to `.pre-transfer-<transfer_id>`, and atomically renames the verified tree into `<to_home>/workspaces/<id>`.
4. **Atomic Postgres cutover** (`gateway/agents/transfer/router.py:commit_cutover_blocking`): locks `agents_meta FOR UPDATE`, re-verifies the full CAS snapshot and absence of open/unsettled impersonations, updates `machine = to_machine` while clearing `runtime_owner = NULL` and `runtime_generation = NULL` (preserving `status` and `termination_source`), closes open `agent_pages`, deletes stale `agent_shell_ttls`, records the `"transfer"` audit event (`base/events/declarations/audit.py`), and inserts a passive `system_note` (`NoteTag.CONTEXT`, `passive = True`) into `inbound_messages` so neither `idling` nor `terminated` agents are woken by the transfer itself.

If the CAS check or any pre-cutover step fails, `workspace_transfer_abort` rolls `<to_home>/workspaces/<id>` back to its `.pre-transfer` backup and `<from_home>/workspaces/<id>` remains intact. Once Postgres commits, `workspace_transfer_finalize` retires `<from_home>/workspaces/<id>` to `.transferred-<transfer_id>`, releases source browser pages, and removes the target backup; post-commit cleanup warnings degrade into `warnings` on `TransferAgentResponse` without reverting the committed ownership change.
