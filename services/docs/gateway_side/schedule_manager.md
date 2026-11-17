# Schedule Manager — declarative single-round execution

The host-local `schedule-manager` supervises schedules assigned to its machine on
both gateway and agent-runner hosts (`ServiceSpec.capabilities=BOTH`, health
port slot `schedule_manager` = 8122). Co-located split units elect one manager
through `machine_units`, preferring the serving gateway unit and then the lowest
home path. This election applies to every reconcile and request pass.

## Loops

Two sequential loops run under one `TaskGroup` in
`services/wake/schedule_manager/daemon.py`. A loop that raises cancels its sibling
and ends the service; root supervision restarts it. Each loop reports progress
to `/healthz`.

- `reconcile` runs every five seconds, admits due `at`/`cron`/`every` slots, and
  settles or recovers open runs. A waiting schedule consumes no process.
- `requests` runs every second and drains legacy machine-scoped
  `schedule_sync_requests` after observing durable state. Mutations update timing
  directly and require no resident-session convergence.

The manager launches one ordinary subprocess per fire with a finite timeout and
an open `schedule_runs` row as its single-flight guard. On service restart it
adopts a verified live runner or its atomic outcome; a vanished runner without an
outcome settles as an error. `latest.log` remains in the schedule workspace.

The payload executes once with only its script path in `sys.argv`. Its state is
an ordinary workspace file, and its schedule and owner identity come from the
launch environment. The state transitions and locality contract are documented in
[Schedule Manager & Runner](../../../gateway/schedules/docs/schedules.md).

## Entry points

- `services/wake/schedule_manager/daemon.py` — supervised service composition.
- `services/wake/schedule_manager/runner.py --single-round` — per-fire custody.
- `services/wake/schedule_manager/runner.py --payload` — single Python payload.
- The application root supervises the service through the roster's `/healthz`
  identity probe (`services/supervision/roster/healthz.py`).

## Process composition

The executable entry captures one `LoadedCommit` and retains its `CodeVersion`
and `ProcessDbGate`. The work database factory and the lazy telemetry producer
share that gate, so new handles preserve the process admission read budget.
Logging and the existing health endpoint receive the same captured image; a
checkout move cannot relabel the running daemon. The daemon's existing startup,
pidfile, work and bounded-exit order remains unchanged.

The service entry retains its prepared `ConfigBoot` and resolved unit runtime
path. Endpoint and pidfile operations borrow those inputs through
`ServiceEndpoints.from_boot`; each original lookup reads the same live view.
The work and logging database factory calls `db_config_from_boot` at each
handle creation and retains the entry's gate and lazy machine host reader.
Schema checks, health-before-pool ordering and shutdown stay at their original
boundaries. Business-specific configuration slices keep their existing readers.
