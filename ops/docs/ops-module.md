# Ops module

The ops layer owns desired service state, lifecycle operations and deployment
coordination. Its design rationale is recorded in
[the ops decision](../../docs/decisions/runtime/hosts/2026-07-19-ops-k8s-semantics-without-k8s.md).

| Responsibility | Current implementation |
|---|---|
| Service specification | not in `ops`: the service roster, its `ServiceSpec` contract and gating live in `services/supervision/roster/` (the supervised services own their own declarations) |
| Observation and status | `ops/cluster_status/` (host snapshot, `pause.py` posture, `schema_mismatch` diagnosis) |
| Alerts | `ops/alerts.py` (the alert row and its SSE publish, shared by the gateway alert endpoints and the events-maintenance reconciler) |
| Agent lifecycle | `ops/agents/` (birth and wake); `ops/lifecycle/` (lifecycle RPC ops) |
| RPC | `ops/rpc_schemas/` (wire vocabulary); `ops/cluster/rpc.py` (gateway client) |
| Op clusters the ops server dispatches | `ops/lifecycle/`, `ops/cluster/operations.py`, `ops/host_config.py`, `ops/inventory.py` |
| Stop and restart | CLI stop orchestration; the agent host settles its own running turns on SIGTERM |
| Fleet update | `cli/fleet_update.py` (down and up scripts per unit, gated by the code version) |

Each package door is the module it grew from; `ops.agents`,
`ops.rpc_schemas` and `ops.cluster.operations_status`
kept their import paths. Module names never repeat the package name, so the
op clusters read `ops.lifecycle`, `ops.cluster.operations.operations`, `ops.host_config`
and `ops.inventory`. `ops/private_files/` owns the private-file
verifier and its manifest.
Its operator entry point remains `python -m ops.private_files`.

`build_services()` supplies the application root manifest and local status roster.
A plugin service may bind a frozen, non-secret `plugin_config` snapshot to its
`ServiceSpec` and gate. The root launcher serializes that instance into the unit
environment; the existing manifest digest includes it. Daemon boot validates the
complete packet against its declared schema instead of rereading mutable disk.
Malformed, mismatched or incomplete packets fail; absent packets identify a
direct development invocation. Cluster bootstrap cannot replace this service
birth carrier. Authoritative files remain in `config_inputs` when present.
The [checked service inventory](../../services/supervision/roster/docs/service-roster.md) lives beside the roster;
its sentinel table is validated against that registration in both directions.
Agent-runner units execute agents inside one agent host. There is no per-agent
process launcher or restarter service. Native service sessions and the
`pty-sessions` service's persistent shells remain separate execution resources.

The application root owns service supervision. There is no controller manager,
background checkout/update trigger or scheduled updater reaper.

Stop, restart and update stop the agent host first and wait for it: on SIGTERM it
writes a restart for each running turn, waits for the turns to reach it and exits.
The stop then closes the selected local services, PTYs and data plane; restart
keeps the data plane and browser. The ops server drains the requests it already
admitted before it exits (`agent_ops/in_flight.py`). Timeout fails without implicit
force. The complete operator contract is in
[graceful maintenance](../../docs/conventions/operations/graceful-maintenance.md).

A fleet update is `python -m cli.fleet_update`. What the update path still lacks
is recorded in the
[unified lifecycle plan](../../future/infra/lifecycle/unified-cluster-lifecycle.md).

The native OS unit supervises the application root, which owns its service
subprocesses. Agent shells are held by the `pty-sessions` service. Stop verifies captured
process identity before signalling.

The `ops.pty_close_notices` one-shot child reads its staged batch before
constructing any database handle or capturing its source image. A nonempty batch
uses one captured image and a nonexempt process database gate; an unknown image
stays unknown rather than using the checkout's current HEAD. The child retains
failed notices for the next start and clears only a successfully written batch.

Notice tests construct their recording and refusal handles through the public
Database configuration constructor with the same process gate and direct resolver
as their admitted database fixture. The recording handle still opens real SQL
connections and checks direct selection and closure. An empty batch must never
enter connect: a forbidden-entry counter remains observable even when the notice
writer's failure isolation would turn an empty failed batch into an empty list.
These observations do not certify absence of background shell processes.

The import boundary is `base < ops < {gateway, cli}`. The supported RPC
vocabulary lives in the `ops/rpc_schemas/` door; focused agent contracts live in
its `terminate` and `content` submodules. Gateway-only
schemas stay in `gateway/schemas/`.
The [content bound](../rpc_schemas/docs/content.meiva.okf.md) is a shared character
constraint for prompts and replies, separate from transport and model budgets.
The client and daemon reject unknown kinds before machine lookup, dedupe, or dispatch.
[Termination test evidence](../lifecycle/docs/termination-tests.meiva.okf.md) describes
the transaction, row-lock and native shell-session checks.
Cluster identity remains the installed home path, resolved before runtime
configuration construction.

The host status snapshot's `paused` field is true whenever the host cannot claim
readiness: its serving gate has not reached `start-serving` (a start in flight, a stop
that has begun, or a failed start that parked the gate), or its database snapshot
failed. `paused_reason` names the first true clause (`no_state` / `startup`), so a
failed start that parked the serving gate reads apart from an unreadable database.
A missing DB snapshot cannot claim ready. The responding
ops process SHA and source checkout SHA remain read-only status metadata;
these fields do not certify every sibling daemon's running code.

`ops/cluster_status/schema_mismatch.py` compares the applied migration set
with the running image's required set. Wheel runtimes use installed SQL metadata without Git.
The status contract contains the diagnosis kind, machine and detail; it has no
Git-pin category, watchdog counters or held-service projection. Ordinary stop
remains independent of this read-only schema diagnosis. Invalid database catalogs or image migration layouts
report `invalid-migration-layout`; query and connection failures report
`unavailable`. Only a successful comparison of equal sets returns no diagnosis.
The batched host snapshot also reports an unavailable comparison when its shared
database read fails, without retrying through another connection.

## Birth transaction ownership

`base.agents.birth.spawn.create_agent_row` retains its public transaction and
post-commit announcement contract. Its cursor-owned SQL writer is
`base.agents.birth.transaction.insert_agent_birth`; see
[creation transaction primitives](../../base/agents/tasks/docs/creation-transactions.meiva.okf.md).
The writer opens no connection, commits nothing and performs no launch.
A keyed birth retains its original snapshot in this same transaction; see [[gateway/agents/docs/guarded-drafts.meiva.okf.md]].

## Host configuration and inventory proof

`tests/test_operations_config.py` exercises host reads and reducer writes through
real unit files. A plugin config face and persisted image provide a sensitive
host field, so both value masking and omission from raw overrides are observable
without replacing the panel metadata owner. Existing validators decide valid and
invalid writes, including atomic rejection and preservation of unnamed fields.

`tests/test_inventory_ops.py` reads the real merged built-in and machine MCP
declarations and their enabled overlay. Invalid declarations fail before writes;
disabled servers remain visible in inventory. A child process fixes display
environment before importing the application and calls the normal operations
with the existing `Host` port. It verifies browser capability hints and rejection
of enabling a display-dependent MCP server on a headless host, while allowing
that server to be disabled.

## Agent command view evidence

The command view tests use persisted agent config and checkpoint rows, registered
skill files and the machine MCP config and enable overlay. They retain the real
built-in file commands (`compact`, `recap`) in exact command-list assertions.
Unregistered skill directories stay invisible; narrowing preserves file commands
and selects skills under the dash/underscore fold. Consecutive requests for
different agents must not retain a previous project skill root. The MCP fixtures
explicitly disable built-in servers through the persisted enable overlay before
asserting machine-specific names and default-on behavior.

## Cluster RPC client

`cluster/rpc.py` keeps machine lookup and its error translation in the
`dispatch_to_machine` composition root. A missing registered machine raises
`ClusterOpTargetAbsent`, a definitive absence that terminal-state consumers can
handle separately. It remains a `ClusterOpUnreachable` subtype for existing
transport handlers. A registered machine without an ops URL is unreachable;
neither condition starts HTTP or reads credentials. Unknown operation kinds
fail before lookup, credential access or idempotency-key construction.
`dispatch_to_url` accepts an already resolved address and needs no database;
fleet-update compensation and machine probes can use it during a database
outage.

`RpcClient` owns one dispatch's timeout and retry policy with required HTTP,
credential, timer and jitter ports. The system constructor supplies the existing
httpx client, `SYSTEM_TIMERS` and current settings. Its credential factory reads
current gateway access on every attempt, including retries; authentication
therefore retains its existing refresh lifetime. Each HTTP attempt ignores
system proxy configuration (`trust_env=False`) because cluster peers are
private-network addresses. Its connect timeout is `min(10, timeout_s)`, so a
three-second probe cannot spend ten seconds connecting.

Transient HTTP transport errors and 5xx responses retry with bounded exponential
backoff: `min(0.5 * 2**attempt, 4)` seconds, jittered by 0.5–1.5. The configured
retry count denotes extra attempts; a negative count still makes one attempt.
Business failures, 4xx responses and invalid response envelopes are terminal.
Only `completed` and `failed` wire statuses are accepted. Completed responses
return the result; failed responses raise `ClusterOpFailed` with the original
failure result for consumers to translate. The internal retry signal never
escapes: exhaustion raises `ClusterOpUnreachable` with the last transport cause.
Probe exhaustion logs at DEBUG; other operation failures remain WARNING.
`worst_case_dispatch_seconds` derives attempt and maximum backoff time from the
same configuration for callers that own an overall deadline.

Spawn-launch and lifecycle operations retain one idempotency key across every
attempt. A spawn-launch business id produces a key scoped to its target and
canonical complete payload, so repeated launch dispatches can replay the same
outcome. Lifecycle calls and payloads without a business id get a fresh key per
dispatch unless the caller supplies one; repeating reconciliation must not
replay a previous failed result. Operations without transport dedupe omit the
key from the wire envelope. The agent-ops server owns deduplication and outcome
replay; the client does not reimplement it.

The focused RPC tests drive the actual retry/error classifier through httpx's
transport port and `ManualTimers`. Root coverage reads real machine rows and
reaches a scoped local HTTP server; credential rotation verifies the order of
credential reads and HTTP attempts while preserving the key. A child process
uses the real gateway configuration profile and temporary unit `.env` to verify
system timeout/retry defaults without replacing settings attributes.
