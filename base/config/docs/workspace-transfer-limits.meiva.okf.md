---
type: doc
title: Workspace transfer limits
description: The two ceilings a whole-agent workspace transfer enforces — configuration, defaults, and the budget check that runs before packing.
tags: [configuration]
---

# Workspace transfer limits

A cross-machine workspace transfer is bounded on two axes, and both are cluster config
(`settings.transfer`): `max_workspace_files` counts entries — files, directories and symlinks, so
the number that matters for a bundle carrying one tar member per entry — and `max_workspace_bytes`
counts the original file bytes a workspace holds, not the compressed bundle it becomes
([decision](../../../docs/decisions/runtime/config/2026-11-04-workspace-transfer-ceilings-configurable.md)).

All three process kinds that enforce a transfer read the same pair: the source runner while it
packs, the gateway while it holds the framed `MEIVA_WS_V1` bundle in memory, and the target runner
before it extracts. The pair is cluster-pinned for exactly that reason — a transfer must not pack
what its receiver would refuse, and the gateway's stream bound (the byte ceiling plus 24 MiB of
framing and manifest) is derived from it rather than stored.

The ceilings travel as one frozen value
(`ops.agent_transfer.budget.WorkspaceTransferLimits`), built once per process from the config
authority (`ops.agent_transfer/limits.py`). No check reads process settings, and this module holds
no copy of the numbers: the settings domain is their only owner, so a configured ceiling reaches
every check, every refusal and both ends of the stream.

`archive_io.preflight_workspace_budget` measures a workspace before the export does anything
irreversible with it: one `walk` plus `lstat` per entry, reading no file content, summing both axes
and refusing when either is over. A refusal names what the workspace holds on both axes, the
effective ceiling of each, and the setting an operator would raise.
