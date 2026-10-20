---
type: doc
title: Machine Identity & Capabilities
description: '`base.cluster.machine` — this host''s name and capability SET (`gateway` / `agent-runner` / `observability-station`, any subset). A host runs the union of its capabilities'' services; the `machines` table is the cluster view of both labels.'
tags:
- base
- library
- cluster
---

# Machine Identity & Capabilities

## What it is

Two labels define a host in a multi-machine deployment, both resolved by
`base.cluster.machine`:

- **`MachineIdentity.name()`** — the stable identifier. The memory git branch
  (`machine-<name>`), `agents_meta.machine`, and the `machines` table PK all read
  from it. Deliberately **not** `socket.gethostname()`, which drifts on macOS
  when switching wifi.
- **`MachineIdentity.role()`** — a **frozenset** of capabilities, not a single role.
  Test the required capability with membership in this owner's set.

Source for both: the composition root's `ConfigBoot`, supplied by
`base.cluster.machine.boot.machine_from_boot(config)`, else fail loud
(`MachineNameMissing` / `MachineRoleMissing`); there is no second channel. The role comes from three
*independent* booleans — `MEIVA_MACHINE_SERVE_GATEWAY` /
`MEIVA_MACHINE_SERVE_AGENT_RUNNER` / `MEIVA_MACHINE_SERVE_OBSERVABILITY_STATION` —
so a single box is not a third role, just the relevant flags true.

There is **no TTY prompt**: `meiva init` records these from its flags, and a
missing value prints an actionable error and exits 1. An agent calling `meiva`
(e.g. `meiva cluster status`) has no TTY and would hang on a prompt.

## The capabilities

`MachineIdentity` accepts four explicit lazy readers for name, capabilities,
reachable host, and description. Construction performs no configuration reads.
Its value module is settings-free; only the explicit startup factory imports
`ConfigBoot`, so importing a machine or pipeline type does not admit a home.
Each field has its own cache; resolving capabilities does not require a machine
name, and an absent description is cached too. `reset()` invalidates only that
owner, so a configuration repair can take effect without touching another
process context. `roles_from_flags` owns capability validation for roots that
provide configuration readers.


`MeivaContext.machine` carries the exact root-owned identity. SDK machine reads use
`meiva.context.require_machine()` and reject an absent context or a partial context
without a machine. An explicit context may carry a machine without an agent;
reading its name/description constructs no plugins, database or execution state.
Graph turns borrow the host's owner. The execution child reconstructs its own
owner from its delivered configuration, rather than serializing live caches.

An independent external attachment captures configuration once and owns a fresh
machine; a borrowed attachment uses the prior context's exact owner. Lease
validation happens before binding, and detach restores the prior context without
resetting its caches. Each CLI invocation owns a fresh command identity; a
successful configuration repair resets that command's owner before the start
identity check. Durable names and cluster role tokens remain unchanged.

| Capability | Owns | Data plane it uses |
|---|---|---|
| `gateway` | the HTTP gateway + this cluster's Postgres / Redis + the gateway-side daemons | its own local instances |
| `agent-runner` | the agent host + the ops server | its own local instances when the host is also `gateway`; otherwise a gateway node's, via `MEIVA_DB_URL` / `MEIVA_REDIS_URL` in its `.env` |
| `observability-station` | the native LGTM observability backends (Loki / Prometheus / Grafana) — the declarative form of the `$MEIVA_HOME/lgtm-host` marker | none of its own — native processes on this host; Grafana dials Loki / Prometheus / Postgres over the host loopback |

A host runs the **union** of its capabilities' services — the per-service
capability declaration is `ServiceSpec.capabilities` in `services/supervision/roster/spec.py`, and the
resulting roster is documented in [[services/docs/services.meiva.okf.md]]. A **single box**
(`gateway,agent-runner`) is therefore not a special case in the code, just the
host with several non-empty capability sets and the loopback reachable address.

**Only one gateway per cluster.** The labeler / heartbeat / report /
memory-indexer daemons would race on the same DB rows if two ran, and one
frontend is sufficient.

## The `machines` table

Moved to [[machines-table.meiva.okf.md]] — the roster row, its addressed
`gateway_url`, the maintenance window a stop can declare, and the spawn-target
invariant.
## Notes

- Any endpoint returning or fanning out per-machine data must declare its
  role scope; the classification rule is in
  `docs/conventions/python-conventions.md`.
- Enrolling a split runner (`meiva start`) does not birth a cluster — its cluster
  identity **is** the gateway URL + the capability bundle it installed.
- `gateway_auth_headers(access)` presents `gateway_bearer(access)`: this process's delivered machine
  API token (`MEIVA_API_TOKEN`), else the human secret of an operator or gateway-profile
  process; a process launched with the agent or runner profile, a set secret and no token raises
  `GatewayApiTokenMissing` (a remote-managed plane delivers none, so it is exempt);
  empty when open.

## Key Dependencies

- [[services/docs/services.meiva.okf.md]] — which services each capability contributes
- [[base/cluster/docs/paths.meiva.okf.md]] — where the home's `.env` lives
- [[cli/docs/cli.meiva.okf.md]] — `meiva start`, which persists these labels

Gateway access is a value, `GatewayAccess(gateway_url, cluster_secret, remote_plane)`, built by a root
from its configuration (`base/cluster/machine/boot.py:gateway_access_from_boot`).
`gateway_api_base(gateway_url)`, `gateway_bearer(access)`, `gateway_auth_headers(access)` and
`daemon_acceptance(access)` read no settings; `resolve_gateway_bearer` keeps token-first admission.
A caller with no root yet calls `base/cluster/machine/ambient.py:gateway_access_from_settings` and labels it
`# ambient: <batch>`; the list only shrinks and the module goes with the last call.
