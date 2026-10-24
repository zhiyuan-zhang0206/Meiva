# Ops module

The ops layer owns desired service state, lifecycle operations and deployment
coordination. Its design rationale is recorded in
[the ops decision](../../docs/decisions/runtime/hosts/2026-07-19-ops-k8s-semantics-without-k8s.md).

| Responsibility | Current implementation |
|---|---|
| Service specification | not in `ops`: the service roster, its `ServiceSpec` contract and gating live in `services/supervision/roster/` (the supervised services own their own declarations) |
| Observation and status | `ops/cluster_status/` (host snapshot and the persistent-shell operations, `schema_mismatch` diagnosis) |
| Alerts | `ops/alerts.py` (the alert row and its SSE publish, shared by the gateway alert endpoints and the events-maintenance reconciler) |
| `.env` write audit | `ops/env_audit/` ([audit and integrity guard](env-audit.meiva.okf.md)); the shell reads files and the registry, strict modules decide redaction, the record and the integrity plan |
| Upload delivery receiver | `ops/upload_delivery/` (`copy_batch` takes a `ReceiverConfig` of values and executes the plan `copy_plan` returns; `receive` is the daemon's entry that reads the process home, gateway access and quota constants once per call) |
| Shell close notices | `ops/pty_close_notices/` (`notice` and `delivery_plan` decide which closed sessions notify which live owners; the shell writes the batch in one transaction) |
| Plugin and MCP inventory | `ops/inventory/` (`toggles` judges the all-or-nothing toggle request; the shell reads the host and writes the toggles) |
| Private production files | `ops/private_files/` (read-only verifier of the registered files; `verdicts` and `report` judge and render what the shell found) |
| Agent lifecycle | `ops/agents/` (birth and wake); `ops/lifecycle/` (lifecycle RPC ops) |
| RPC | `ops/rpc_schemas/` (wire vocabulary); `ops/cluster/rpc.py` (gateway client) and `ops/cluster/rpc_plans.py` (its decisions) |
| Op clusters the ops server dispatches | `ops/lifecycle/`, `ops/cluster/operations.py`, `ops/host_config.py`, `ops/inventory/` |
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

`meiva stop` writes its shell-close notices itself over one short database connection opened
and closed inside its `shell-sessions` phase (`ops.pty_close_notices.write_notices`): the whole
batch commits in one transaction or not at all, a notice for a terminated owner is dropped, and
a batch that cannot be written is returned to the caller to report. Nothing is staged on disk
for the next start.

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

### Host snapshot shape

`ops/cluster_status/__init__.py` is the snapshot's shell and the package's composition
root for the ambient reads it needs: it reads this host's facts once (the daemon pidfile, the
session listing, the central-database snapshot, the resource sample, the serving gate, the
checkout HEAD, the root supervisor, the cluster clock) and hands them as a frozen `StatusFacts`
to `report.assemble_status`, which decides what the snapshot says: the three capability flags,
the session counts and agent groups, and the `paused` verdict with its reason. `listing`
turns what the session backends listed into session rows at one given instant;
`shell_plans` picks an agent's shells out of a listing, picks what an owner-level kill-all
targets (below the cutoff, page-server sessions spared) and judges a kill's outcome;
`model` compares the two migration sets and words a failure to read them. These four
modules are strict: values in, values out. `sessions` holds the effects over them: it asks
the service backend (the native supervisor) and the shell backend (the pty-sessions service)
for their sessions and calls the backend to capture or kill. The status snapshot degrades past
a backend that is down; an operation keyed on "is this shell there" (probe, capture, kill,
kill-all) fails instead, because a failed listing read as "no shells" would make the TTL
reaper take a live shell for already gone.

The snapshot tests run against real resources: the throwaway Postgres (agent rows, a real
`ConnectionPool` that is closed or held for the degraded reads, rolled-back edits to
`schema_migrations`), pidfiles under the test home, and the shells of a real pty-sessions
service. Two properties have no test: that the resource sample overlaps the database read (a
timing property no real resource can force), and that a failed psutil read degrades to no
sample (psutil cannot be made to fail).

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

`cluster/rpc.py` keeps machine lookup and its error translation in
`RpcClient.dispatch_to_machine`. A missing registered machine raises
`ClusterOpTargetAbsent`, a definitive absence that terminal-state consumers can
handle separately. It remains a `ClusterOpUnreachable` subtype for existing
transport handlers. A registered machine without an ops URL is unreachable;
neither condition starts HTTP or reads credentials. Unknown operation kinds
fail before lookup, credential access or idempotency-key construction.
`dispatch_to_url` accepts an already resolved address and needs no database;
fleet-update compensation and machine probes can use it during a database
outage.

`RpcClient` owns one dispatch's transport: a timeout and retry `RpcConfig`, the
retry loop's `Timers` and a credential read. `RpcClient.system()` is the one
composition entry that reads the process settings (the RPC timeout and retry count,
and the gateway access); the module-level `dispatch_to_machine` and `dispatch_to_url`
are its one-shot shorthand for callers that hold no client of their own. Both
shorthand entries reject an unknown operation kind before constructing the system
client or reading configuration. A caller that builds a client passes it to
`RpcClient.dispatch_to_machine` instead.
The credential read is a required lazy factory, called on every attempt including
retries, so a credential refreshed between attempts is the one presented;
authentication therefore retains its existing refresh lifetime. The HTTP client is
the real `httpx.AsyncClient`, built per attempt, and ignores system proxy
configuration (`trust_env=False`) because cluster peers are private-network
addresses. Its connect timeout is `min(10, timeout_s)`, so a three-second probe
cannot spend ten seconds connecting.

`cluster/rpc_plans.py` decides from values what the client does: the dispatch
budget (a caller's deadline and retry count over the configured ones), the wait
before a retry, the connect deadline, what an HTTP status means and the business
idempotency key. The jitter factor is drawn once per retry by the loop and passed
in, so the wait is a pure function of the attempt and the factor.

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

The focused RPC tests drive the client against a scripted HTTP listener on loopback
with the real `httpx` client, so a refused connection, a response lost after the op
ran, a 5xx and a malformed body are the ones the wire produces; the retry loop runs
on `ManualTimers`, which records each wait instead of sleeping, and the waits are
asserted to lie within the jittered backoff. The decisions are asserted on values in
`test_cluster_rpc_plans.py`. Root coverage reads real machine rows and reaches the
listener; credential rotation checks that each attempt presents the credential read
for it while the key is preserved. A child process uses the real gateway
configuration profile and temporary unit `.env` to verify that `RpcClient.system()`
reads the configured timeout, retry count and credential, and that the client ignores
a dead system proxy, without replacing settings attributes.
