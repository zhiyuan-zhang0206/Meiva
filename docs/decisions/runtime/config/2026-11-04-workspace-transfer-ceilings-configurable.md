# Workspace-transfer ceilings become config, with a budget check before packing

## Context

A whole-agent cross-machine transfer packs `<home>/workspaces/<agent_id>` into a `MEIVA_WS_V1`
bundle and carries it to another machine. Since the feature landed (2026-11-01) two hardcoded
constants bounded it: 256 MiB of original file bytes and 20,000 entries
(`ops/agent_transfer/bundle_plan.py`). Neither number had a recorded reason — the pull request that
introduced them still described 2 GiB / 100,000 entries, and no commit explains the tightening — and
both were enforced by string literals in the error messages, so the numbers could only change in
code.

On 2026-11-03 a transfer of a real workspace was refused with
`workspace exceeds the 20000-entry transfer limit` (`retryable: false`). That workspace held 595,471
entries and 22 GB of files — 30x over the entry ceiling — and compressed to 78 MB. The ceiling was
right to refuse it; the repair was not obvious, because nothing in the refusal said what the workspace
held, which axis it failed, or what an operator could change. And the refusal arrived after the
source had already killed the agent's persistent shells, with the workspace's bytes still to be read.

## Decision

The pair becomes `settings.transfer` (`base/config/domains/transfer.py`): `max_workspace_files`
(entries) and `max_workspace_bytes` (original file bytes), cluster-pinned with `restart_required`
`all`, because three process kinds enforce them — the source runner that packs, the gateway that
holds the framed bundle, and the target runner that extracts. The defaults stay 20,000 / 256 MiB.

The two ceilings travel as one frozen value (`ops.agent_transfer.budget.WorkspaceTransferLimits`),
read once per process from the config authority (`ops/agent_transfer/limits.py`). The numbers live
only in the settings domain: `bundle_plan` keeps no copy, so every check, every error message and the
gateway's 24 MiB-above-the-ceiling stream bound follow whatever is configured. A refusal names both
axes — what the workspace holds and the effective ceiling — and the setting that moves it.

The source runner also measures the workspace before it does anything else with it
(`archive_io.preflight_workspace_budget`): one `walk`/`lstat` pass that reads no file content and
sums both axes, then refuses. It runs before the export's own residual-shell kill, and for the entry
axis it turns a refusal that used to arrive after hashing gigabytes into one that arrives after a
walk.

## Alternatives rejected

- Raise the constants. A larger ceiling only moves the cliff, and one RPC call that moves a 22 GB
  workspace in a single framed stream is a memory and timeout problem of its own. The question the
  incident raised is what a workspace may contain, which stays a policy.
- Change the transport (compress the tar, page the bundle, stream it to disk). Real, and a separate
  change: it would not have made this refusal legible, and the entry axis is unaffected by
  compression.
- A per-agent ceiling. The transfer is driven by an operator from Gateway to two runner processes;
  neither end has the agent's own resolved slices, and a per-agent bound would make a machine's
  memory ceiling depend on which agent is being moved.
- A `gateway`-capability domain. The source runner is the end that must refuse early, and a
  gateway-capability field is exactly what the consumption guard forbids runner code to read;
  `common` states the cluster-wide ownership these checks have.
- Keep the numbers in `bundle_plan` and pass them in from settings at the call site. Two copies of one
  policy, and the error message would still quote whichever copy the raiser held.

## Consequences

Operators raise a ceiling with `MEIVA_TRANSFER_MAX_WORKSPACE_FILES` /
`MEIVA_TRANSFER_MAX_WORKSPACE_BYTES` in the cluster config, and the change takes effect when the
gateway and the runner daemons restart. The refusal for an over-budget workspace is now the first
thing an export answers instead of its last, and it carries the three numbers and the setting name.

Known gap this leaves: an idling agent is still terminated (step 1) before the source measures its
workspace, so an over-budget transfer leaves it terminated on the source machine. Closing that needs
a budget probe the gateway can call during precheck, ahead of step 1 — a new ops arm, deliberately
not part of this change.
