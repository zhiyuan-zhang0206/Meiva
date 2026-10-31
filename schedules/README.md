# Schedules

Machine-placed schedules for cluster agents: a schedule is a persistent,
supervised session (a `script` + the `command` that runs it) owned by the
`schedule-manager` service's ScheduleManager on the schedule's assigned machine —
see `services/wake/schedule_manager/manager.py`. Manage them via
`meiva schedules ...` (thin client over `/api/schedules`) or the
`/control/schedules` page.

## Built-in schedules

Meiva ships a set of built-in schedules, declared in
[`manifest.json`](manifest.json) next to their script templates in this
directory. The manifest is the single expression of the built-in policy
(user ruling 2026-08-11, pre-open-source):

| Schedule | Script | Class | Default |
|----------|--------|-------|---------|
| `c9-daily-report` | `c9-daily-report-schedule.py` | product | **enabled** |
| `debt-sweep-daily` | `debt-sweep-daily-schedule.py` | product | **enabled** |
| `dev-ci-metrics` | `dev-ci-metrics-schedule.py` | product | **enabled** |
| `adversarial-eval-weekly` | `adversarial-eval-weekly-schedule.py` | product | **enabled** |
| `memory-arbiter` | `memory-steward-schedule.py` | product | **enabled** |
| `self-evolution-daily` | `self-evolution-daily-schedule.py` | product | **enabled** |
| `self-evolution-weekly` | `self-evolution-weekly-schedule.py` | product | **enabled** |
| `model-update-tracker` | `model-update-tracker-schedule.py` | product | **enabled** |
| `trace-ship-tempo` | `trace-ship-tempo-schedule.py` | product | **enabled** |
| `db-write-stall-watch` | `db-write-stall-watch-schedule.py` | operator | disabled |
| `sched-dry-import-weekly` | `sched-dry-import-weekly-schedule.py` | operator | disabled |
| `log-cred-mask-monthly` | `log-cred-mask-monthly-schedule.py` | operator | disabled |
| `agent-watch` | `agent-watch-schedule.py` | operator | disabled |

`dev-ci-metrics` also watches the timed workflows a pull request never blocks: it
messages the P0 lead when one of them fails two runs in a row (`_SCHEDULED_WORKFLOWS`), because a red nightly is otherwise silent.

- **product** schedules (adversarial evaluation, self-evolution, memory, model
  tracking, debt clearing, CI observability, trace shipping) are Meiva's own
  improvement loops — they ship and start by default.
- **operator** schedules (cluster-operator tooling) ship with the product but
  start **disabled**: they exist so they are discoverable, and start only when
  the operator enables them. `db-write-stall-watch` is the first: three
  consecutive minutes under 20 xacts/s with an average query time above 300 ms
  means the write path has stopped completing transactions, which is the
  root-cause signal an agent-host crash wave follows. It wakes the operator and
  the P0 lead only — never a user notification path — and stops with
  `meiva schedules stop db-write-stall-watch`.
- `agent-watch` is the other half of the same problem (#5594): a duty that
  stops taking its rounds looks like a duty with nothing to report. Every ten
  minutes it reads the signals a stopped duty leaves behind — no live shell
  session, no real turn for longer than its cadence allows, a schedule the
  agent owns that stopped firing on its own cadence — and climbs one ladder:
  wake the agent (the cheapest rebuild), tell the P0 lead with the raw values,
  then ask for the user. A reading the host cannot take is carried as `blind`
  and never counted as healthy; a terminated agent is finished, not silent, so
  the sentinel never resurrects a duty the user ended.

Built-in slots sit in the off-peak trough: the cluster workday peaks at
09:00-12:00 and 14:00-18:00 cluster time, so every daily and weekly fire lands
outside it.

The adversarial weekly batch owns its counted loopback HTTP server in a lexical
scope (`schedules/jobs/adversarial_scenario.py`). One batch-local executor serves
requests while the batch updates the partner document and collects counts. Leaving
the batch, including by an error, shuts down the server, joins the worker and
closes the socket. Server-loop failures propagate to the schedule's existing
batch-error reporting. [The batch](docs/adversarial-eval.meiva.okf.md) and
[the debt sweep](docs/debt-sweep.meiva.okf.md) each keep their decisions in
strict modules under `schedules/plans/` and their effects in shells under
`schedules/jobs/`.

### How built-ins get created

`provision_builtin_schedules(conn, machine=...)` (`base/daemon/schedules/builtin_schedules.py`) creates every
manifest schedule missing from the `schedules` table, with `enabled` taken
from the manifest's `default_enabled` and `machine` set to the gateway's machine. It runs:

1. **at gateway startup lifespan** (before serving traffic) — gated by
   `settings.gateway.provision_builtin_schedules`, and
2. **`meiva schedules provision`** — manual restore, works even while the
   gateway is down.

Provisioning is **idempotent**. A missing row is created. An existing row whose
`script` or `command` differs from the template is **resynced** to it: the DB copy
of a built-in is a snapshot made when the row was created, and a snapshot goes
stale the moment a library function the script calls changes shape (2026-10-03:
`catch_up()` gained a required `db`, 13 stored copies crash-looped). The resync
writes a `schedule_versions` row (`builtin-resync <hash>`, so the replaced text stays
recoverable) and queues the sync request that relaunches a live session onto the
new code. `enabled` and `description` of an existing row are never touched, so
`meiva schedules stop <name>` (or disable in the UI) keeps a built-in around without
running. Delete a built-in and the next provision brings it back with its manifest
default. The checkout is the source of truth for a built-in's code: a hand edit of
its stored script is replaced at the next provision; edit the template instead.
Schedules that are not in the manifest (agent-created ones) are never read or
written by provisioning. Rationale and rejected alternatives:
[decision](../docs/decisions/agents/scheduling/2026-10-03-builtin-schedules-resync-from-repo.md).

### Adding or changing a built-in

Edit the script template and the manifest entry (name, class, default_enabled,
description), PR it, and deploy: the next `schedule-manager` start syncs it into
existing clusters.

Four places change together; each one left out fails a directory-wide gate
rather than a review:

1. **The template.** `tests/components/base/test_schedule_timezone.py` sweeps
   every `*-schedule.py`: at least one timezone argument must be present and
   every one of them must be `cluster_timezone()` (a `timezone=` keyword, a
   `ZoneInfo(...)` argument). A resident sentinel with no cron slot is no
   exception — stamp its own output with `ZoneInfo(cluster_timezone())`. A
   hard-coded IANA name, or a template reading `settings.general.timezone`
   itself, fails the same test. A cron slot passes it as the loop's
   `CronSchedule(..., timezone=cluster_timezone())`; the cron literal stays in the
   template, where the same test reads it.
2. **`meizel.toml`'s `[uses]`.** An import of a sibling directory (a template
   imports `schedules/jobs` for the loop, the entry and the agent calls, and
   `schedules/plans` for the decisions) needs its declared edge, or
   `meizel check` fails the locality gate.
3. **The manifest entry and this table** — name, class, `default_enabled`,
   description.
4. **`base/daemon/schedules/tests/test_builtin_schedules.py`**, whose
   repo-manifest test names every entry's class and the set of schedules that
   start by default. A new entry cannot pass by carrying the right class and the
   wrong default.

A template is thin: its `__main__` block reads the schedule id, the timezone, the
operator's report-recipient variable and the start time once, builds the values its
job needs and calls `run_loop`. What a schedule decides lives in a strict pure module
under `schedules/plans/`, and what it does lives in a shell under `schedules/jobs/`;
[the loop and the agent directory](docs/loop-and-directory.meiva.okf.md) say how. The
C9 job imports its accounting dependency through `scripts.ci.pull_requests.accounting`
where it runs. Materialization changes the script directory, not the installed module
roots (`schedules/tests/test_scripts_entry.py` runs every template from a copy). Tests
live in `schedules/tests/`.

A `.py` template is executed in-process by the runner, which hands it a clean
`sys.argv` — just its own path, exactly what `python <script>` would give it; the
runner's own argv (`python -m services.wake.schedule_manager.runner <id>`) never reaches the
script, so a template's CLI takes its own flags only (keep them usable for
manual runs). On deploy, new manifest entries are
provisioned and changed templates are resynced at the next `schedule-manager`
start; changed `default_enabled` / description only apply to rows that do not
exist yet — an existing cluster's rows keep their operator-set state. To push a
template into a running cluster without a restart of the manager, run
`meiva schedules provision`.

## Machine placement & Multi-machine operation

Every schedule row records a target `machine` (defaulting to the gateway machine if omitted) and an optional `owner_plugin`.

- **Per-machine schedule manager**: `schedule-manager` runs on all cluster nodes (`capabilities=Capabilities.BOTH`, `requires_db=True`, `db_access="runner"`). Each instance reconciles only rows where `machine = self._machine_name`.
- **Split-unit tie-breaker**: On hosts with multiple units sharing a machine name (such as preview environments), `is_active_unit()` inspects `machine_units` and activates the instance on the serving gateway unit (or the lowest lexical home path). Inactive units yield reconcile and sync loops.
- **Session reaping across moves and deletes**: When a schedule is deleted or reassigned to another machine, the old machine reaps its running PTY session without overwriting status or error on the database row.
- **Machine migration**: Moving a schedule via `meiva schedules update <name> --machine <target>` resets `launch_generation = NULL, launch_count = 0, next_launch_at = NULL` and enqueues sync requests for both the old machine (to terminate the existing session) and the new machine (to launch it).
- **Placement status API**: The gateway evaluates placement health via `judge_placement()`:
  - `healthy`: machine is registered and reachable.
  - `unplaced`: schedule row has no machine assigned (`NULL`).
  - `unregistered`: assigned machine does not exist in the `machines` table.
  - `unreachable`: assigned machine has no active heartbeats or units.
  - `stalled`: machine is unreachable and the schedule is currently active or enabled.
- **Cross-machine log capture RPC**: When reading logs (`GET /api/schedules/{id}/logs` or `meiva schedules logs <name>`), if the schedule is placed on a remote machine, the gateway routes a `schedule_capture` cluster operation via RPC to `agent_ops` on that machine to fetch live PTY output.

## Deployment notes

- **The DB is authoritative.** The gateway materializes each schedule's script
  from the `schedules` table to `~/.meiva/schedules/<id>/` on launch; editing
  only the on-disk copy is overwritten at the next launch/restart. Change a
  schedule through `meiva schedules update` / the API, never by editing the
  materialized file.
- **Cron expressions are cluster wall clock.** The built-in scripts read
  `settings.general.timezone` once, through `cluster_timezone()` (`MEIVA_TIMEZONE`, scope
  `cluster-pinned`), and hand it to the loop that computes each slot with `next_fire`,
  so `0 4 * * *` means 04:00 cluster time on every host in the fleet — the host's OS
  timezone never enters. `MEIVA_TIMEZONE` is read once
  at process start, so a changed value needs `meiva schedules restart <id>`.
  A one-off schedule you write yourself should do the same rather than
  hard-coding an IANA name; `next_fire(expr, timezone=None)` computes in UTC.
- **Report recipients are operator knobs.** A daily report resolves its
  recipient from its own `MEIVA_*_REPORT_AGENT` variable when set — a numeric
  agent id, and a malformed value stops the schedule at its start instead of falling
  back — and otherwise from the fixed report label the script declares
  (`_REPORT_LABEL`). The variable is read once, when the script starts.
  `MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT` defaults to agent 228, and an
  explicit empty value skips that report. The debt-clearing worker label comes
  from `MEIVA_DEBT_SWEEP_AGENT_LABEL`.
- **Built-in cron slots are durably claimed.** Templates run
  `schedules.jobs.loop.run_loop()`, which claims each due slot through
  `schedules.jobs.catchup.fire_slot_once()` for online fires. Startup skips missed
  slots; there is no catch-up helper. The `(schedule_id, slot_fire_at)` claim
  commits before the callback, so concurrent runners cannot execute a slot
  twice. A crash after claiming may lose that slot. Callbacks receive the
  claimed UTC slot and payload for report windows.
- **A template change reaches a running cluster at the next `schedule-manager`
  start** (see "How built-ins get created"), or at once through
  `meiva schedules provision`. A resynced **enabled** schedule is relaunched onto the
  new script by the manager's sync request; a disabled one picks it up when it is
  next started. `meiva schedules get <name>` shows which script the row holds. A
  one-off change to a single schedule that is not in the manifest still goes
  through `meiva schedules update <name> --script-file <file>`.
- **Verify in-store scripts when repo code moves.** `meiva schedules verify`
  (needs the gateway: it reads the scripts through `GET /api/schedules`)
  checks every schedule's DB-embedded script — stopped rows included, built-in
  and agent-created alike — against the checkout it runs from: `py_compile`, a
  top-level-imports-only execution in the checkout's runner venv, a name-read
  check (every name the script reads, any scope, must be one it binds — the
  rename leftover the imports alone cannot see; conservative: any binding
  counts, annotations are not read, `from x import *` disables it), and an
  `inspect.signature().bind` of every call the script makes into repo code
  (`schedules.jobs.loop.run_loop(...)`, `inputs.database()`;
  the argument shape only, so a call it cannot bind statically — `*args`, an
  instance method, a rebound name, the plugin-wrapped `meiva.*` SDK — is skipped,
  never guessed). Nothing is started, stopped, or written. The drift classes that
  bit (a module move — e.g. the watcher module's move to `base.daemon.schedules.watcher`
  — leaving stale in-store imports, task #4800; a `shared` -> `base` rename leaving a
  top-level statement reading a name nothing imports — 2026-10-01 audit, three of 16
  copies; a changed signature — `catch_up()` gaining a required `db`, 2026-10-03 —
  leaving calls that raise `TypeError` on the first fire) are caught by its
  `RED id=<id> name=<name> missing=<module|compile-error:<l>:<m>|undefined-name:<l> <name>|call-signature:<l> <call>(): <why>|...>`
  lines before they can fire. `python -m cli.fleet_update down` runs it with the NEW
  checkout's code before stopping anything, and `up` runs it again on the gateway. Exit codes: 0 clean / 1 red / 2 tool error. Run it on the
  host that runs the schedules, or from a dev worktree to check the same table
  against in-development code. `--check-file PATH` checks one script file
  instead of the table (the falsification hook); `--rows-file PATH` sweeps a
  JSON dump of the table (`[[id, name, script], ...]`) with no database access and
  no signal; a non-clean sweep of the live table emits one `schedule_verify_failed`
  event for the Grafana rule to read (`--no-notify` suppresses that for a dry run).
- Deploy a one-off schedule with:

```bash
meiva schedules create <name> --script-file <script.py> [--description "..."]
meiva schedules start <name>
```

The running copy on each host lives in `~/.meiva/schedules/<id>/`; this directory
is the version-controlled source of truth — never edit only the running copy.

## Script repair while the agent host is stopped

Normal schedule edits use `meiva schedules update`. When the stored script must be
replaced while the cluster is stopped, use the reviewed operator tool
`scripts/host_ops/update_schedule_script.py`. It accepts only a schedule ID and
script text, refuses while an agent host holds the home's instance lock, and holds
the same home lifecycle mutex as start/stop.

For first-generation recovery, copy this one file from a fixed reviewed tool
commit to a private operator directory outside the installed checkout. Run it
with the **existing installed source's own interpreter**, explicit home/source
paths, and the old stored script's SHA-256:

```bash
MEIVA_HOME="$H" "$S/.venv/bin/python" "$TOOL" "$ID" \
  --home "$H" --source "$S" --script-file "$PREPARED_SCRIPT" \
  --expected-sha256 "$OLD_SCRIPT_SHA256"
```

`H` is the existing gateway home; `S` is its admitted source checkout recorded
in `start-intent.json`. The tool verifies both the interpreter and the loaded
source, and uses that runtime's normal operator database authority. It neither
copies credentials nor imports application code from the tool's development
checkout. It deliberately reuses the already installed schedule writer's
versioned transaction. This narrow bootstrap dependency on
`gateway.schedules.router._fetch_full_blocking` / `_update_blocking` is tested on
`44d7faaa6dc97e237c7c6780cee26f6b1f13703c` and current source; first deploying a
new public writer would recreate the upgrade gate that this entry resolves.
Ordinary edits continue to use the public CLI after maintenance ends.

An edit preserves ID, name, description, command, enabled state, status, error
and creation time. The existing writer updates script/revision/launch-admission metadata,
adds a `schedule_versions` snapshot, and queues enabled schedules for later
convergence. The tool does not wait for that queue or start/restart sessions.
Prepared text already present is a no-op; any text matching neither the expected
old hash nor the prepared hash is refused. Each invocation commits one row;
a failed batch is partial, and a retry skips completed rows without adding
versions. The old-hash read/write interval is protected by the lifecycle mutex
and the stopped agent host, not an independent concurrent-writer protocol.

Archive the original rows and versions, validate all prepared scripts against
the fixed application target, and compare the complete stored table after the
edits. Verification includes disabled schedules and the callbacks' argument
contracts; the current static verifier does not prove callback arity by checking
only the outer call. Do not start the agent host or launch old schedule code after
installing scripts that require the new callback contract. Finish the normal
whole-cluster update to the approved target before the cluster starts again.

Runner-hosted templates receive immutable `ScheduleInputs` through `runpy.init_globals`:
one loaded image, the entry's database/pipeline builders and its `MachineIdentity`.
A template's `__main__` hands the injected inputs (or `None` when run by hand) to
`open_schedule_entry(inputs, home=...)`: injected inputs are borrowed without closing them or changing
the shared SDK context, and a hand run owns a fresh entry built by
`fresh_schedule_entry(image=..., process=..., home=...)` for the commit it loaded and the home the
script's entry resolved, with its lazy
machine, SDK context, gate and cold writer. Exit restores the prior SDK context and
closes constructed clients within two seconds while preserving an original loop failure.
C9 and the debt sweep open process logging and the event writer only when they report
(`schedules/jobs/telemetry_event.py`); debt dry runs assemble no root.
