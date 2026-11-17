# Schedules

This directory owns the installed schedule payloads and their policy. Every
built-in is a declarative `cron` or `every` run-to-completion program. Stored
third-party `daemon` rows remain supported during their compatibility migration.
Both forms are machine-placed and reconciled by the host-local
`schedule-manager`. Manage them through `meiva.schedules`, `meiva schedules`, or
the `/control/schedules` page. The engine boundary is documented in
[`gateway/schedules/docs/schedules.md`](../gateway/schedules/docs/schedules.md).

## Where an entry script lives

**An entry script sits beside the job it composes, and the manifest addresses it by a relative
path.** `schedules/` is the registry's facade, not a place for scripts: its direct-entry count is
capped at 20 like every other directory, and thirteen entry scripts already fill it. A new schedule
therefore adds `"script": "jobs/watch/thing-schedule.py"` and `"command": "python
jobs/watch/thing-schedule.py"` — the manifest already carries relative names, and
`test_every_builtin_command_runs_the_script_the_manifest_names` proves the command still runs the
file the manifest names. Keeping the entry next to its job also means a reader who opens the entry
sees the module it composes in the same directory.

## Built-in schedules

Meiva ships a set of built-in schedules, declared in
[`manifest.json`](manifest.json) next to their script templates in this
directory. The manifest is the single expression of the built-in policy
(user ruling 2026-08-11, pre-open-source):

| Schedule | Trigger | Class | Default |
|----------|---------|-------|---------|
| `c9-daily-report` | cron `0 5 * * *` | product | **enabled** |
| `debt-sweep-daily` | cron `30 6 * * *` | product | **enabled** |
| `dev-ci-metrics` | cron `20 6 * * *` | product | **enabled** |
| `main-red-watch` | cron `0,5,30,35 * * * *` | product | **enabled** |
| `adversarial-eval-weekly` | cron `0 4 * * 3` | product | **enabled** |
| `memory-arbiter` | cron `0 4,7 * * *` | product | **enabled** |
| `self-evolution-daily` | cron `0 0 * * *` | product | **enabled** |
| `self-evolution-weekly` | cron `0 0 * * 2,5` | product | **enabled** |
| `model-update-tracker` | cron `0 6 * * *` | product | **enabled** |
| `trace-ship-tempo` | cron `*/5 * * * *` | product | **enabled** |
| `harness-cli-update` | cron `30 4 * * *` per machine | product | **enabled** |
| `claude-auth-keepalive` | cron `10 0,6,12,18 * * *` per machine | product | **enabled** |
| `db-write-stall-watch` | every 60 s | operator | disabled |
| `sched-dry-import-weekly` | cron `0 9 * * 1` | operator | disabled |
| `log-cred-mask-monthly` | cron `0 10 15 * *` | operator | disabled |
| `qa-keepalive` | every 600 s | operator | disabled |
| `agent-watch` | cron `*/10 * * * *` | operator | disabled |

`dev-ci-metrics` also watches the timed workflows a pull request never blocks: it
messages the P0 lead when one of them fails two runs in a row (`_SCHEDULED_WORKFLOWS`), because a red nightly is otherwise silent.

`main-red-watch` answers the other half of that question — whether `main` is
green — now that nothing verifies a merged tree. `ci.yml` runs on `pull_request`
plus a nightly full run, so a merge starts no test; the six-hourly `release-gate`,
the nightly verdict and that nightly full run are the only evidence, and on
2026-11-01 two scheduled gate runs went red on `main` with nobody watching. The
full run's own listing is narrowed to `main`: `ci.yml` also runs on every pull
request, and a red branch there is that pull request's business. Every thirty
minutes it reads their newest runs and opens **one** task for the P0 lead per red
that no green run of the same commit explains: a first named red with no later completed run of its workflow records
"no re-check opportunity" and opens no card. The first later opportunity opens
one review card with the exact JUnit result: passed, skipped, absent from the
available bundles, or unreadable evidence. Workflow success alone does not
prove that a case executed. A red that survives into a second run becomes a
confirmed card — the tree need not move for it
(`main` can stand still for days), because a run the watch has not read before
is new evidence even on the same commit — and a green run that exercises it
closes it. A review card is reused if that case later confirms; its next exact
case green can close it even inside an otherwise failed workflow. The latest
round event and Grafana table show the bounded suspect list, age at the snapshot,
opportunities and re-check evidence. Two classes skip that two-run wait and open a
card on the **first** sighting: deterministic audit steps in `scheduled-audits.yml`
(`AUDIT_CHECK_STEPS`, recorded as `FAILURE_CHECK` without incrementing `unnamed`,
since a daily or weekly audit has no flaky test case to re-run), and a red the
watch cannot name at all — when the log carries no failing case and the failing
step is no workflow's declared check, the run is reported at once, because the doubt two runs answer — is this
case flaky? — does not exist when the doubt is whether the watch can read the log
at all. Its fingerprint is the job plus the step names, so one unreadable shape is
one card, and the step name it carries is the entry a mapping would have to gain.
Failures that happen around the suite rather than in it — a container that never
pulled, a disk that filled — are recognised by their own markers and counted,
never becoming a card. Each round
emits `main_red_watch_round`, and that event is persisted rather than lived in
the mirror alone: a second trigger five minutes after each round reads the
newest one back and opens a card for the lead when it is more than an hour old,
so a watch that stopped reporting is a card rather than silence. A stream the
watch has only just started reading gets one threshold of grace before its
silence is reported — a nightly added today has no run on record because its
cron has not come round yet — and a stream that cannot be read at all (a
renamed workflow, a busy API) is one named note in the round, never the end of
the round or of the process that would report it next time.

JUnit evidence accepts both raw `test-results-` and final `verdict-junit-`
publications. Both readers verify the current GitHub attempt before selecting
its newest same-name artifact. Pytest `-aN.xml` denotes retries within that
attempt; it does not identify a GitHub rerun. Missing publication ownership
remains unknown.

One unreadable stream is a note. A round that cannot read **two or more** of its
streams, or a single stream that has been unreadable for **six consecutive
rounds** (three hours at this cadence), is a card: while that window lasts, a
red on `main` opens nothing, so an empty card list means "nobody was reading",
not "nothing is red". The card names the streams, quotes what each one answered
and says that anything merged inside the window is unjudged rather than green;
it closes on the first round that reads every stream again, and its closing line
records the interval and whether the runs that landed inside it were actually
re-read — a listing that cannot reach back over the window is reported as `NOT
covered`, never as covered. One window is one card however many streams join it,
and a single stream failing once stays a note, so a flaky API never becomes
furniture on the board.

Three things it reads run narrow checks rather than the test population: the
`main-push.yml` gate (two checks), `scheduled-audits.yml` (the weekly dependency
audit and nightly full-history secret scan, also watched for liveness under
`NIGHTLY_STALE_AFTER`), and the `data plane (macOS)` job inside the nightly
`ci.yml` verdict (two test directories, on a hosted Mac). Their reds are
cards like any other, but they retire only on evidence that exercises them: a run
that never collected the check is not evidence that it passes, and without that split
the next Linux nightly — which runs the whole population without ever touching
macOS or historical secret scans — would retire the card while the red stood. The failure and the entry
carry that fact (`covers_full_suite`), and non-full-suite green runs also check
`RunFacts.green_jobs` against `entry.job` so a multi-cron workflow run where one
audit job succeeded while the other was `skipped` never closes the skipped job's card.

- **product** schedules (adversarial evaluation, self-evolution, memory, model
  tracking, debt clearing, CI observability, trace shipping, harness CLI convergence) are Meiva's own
  improvement loops — they ship and start by default.
- `harness-cli-update` runs at 04:30 cluster time with `placement: "per_machine"`. It provisions
  a schedule row (`harness-cli-update:<machine>`) on every registered runner host (`role @> '{agent-runner}'`).
  Each machine independently converges its locally installed external harness CLIs (Claude Code, OpenAI Codex,
  Antigravity, Droid) to the catalog versions pinned in this checkout, writing non-disruptive update logs to
  `$MEIVA_HOME/run/harness-cli-update.log` and emitting `harness_cli_update_round` telemetry without waking agents.
- `claude-auth-keepalive` runs every six hours (`10 0,6,12,18 * * *` cluster time, off the workday peaks) with `placement: "per_machine"` (rows
  `claude-auth-keepalive:<machine>` on every `agent-runner` host, like `harness-cli-update`). Each
  machine runs one minimal `claude -p` (model `haiku`, no tools, no session persistence, fixed
  prompt on stdin, 120 s timeout) so the CLI refreshes its own login and writes the rotated
  credentials back; a machine that goes days without a Claude Code run would otherwise let the
  access token expire (the `meiva_usage` Claude card then waits on it) and eventually lose the login.
  The schedule never calls the OAuth refresh endpoint and never reads or writes a credentials file:
  the refresh token is single-use and a second refresher would race a running CLI for it. It checks
  `claude auth status --json` first and makes no model call when `loggedIn` is false. Both child processes run in the user home's directory with
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN` removed from their environment, so the CLI
  uses the subscription login (and bills no API key) and loads no project settings from the schedule's working directory. A round never
  raises: a missing executable (`claude_not_found`), a logged-out machine (`not_logged_in`), a
  non-zero exit (`nonzero_exit`) or a timeout (`timeout`) is printed to the schedule log with the
  exit code and the last lines of stderr (credential-shaped text masked) and emitted as
  `claude_auth_keepalive_round` with `status="failed"`; the round does not retry, because every
  retry would be another model call for a condition that does not change in minutes. A non-production
  identity skips, as `harness-cli-update` does.
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
batch-error reporting. [The batch](docs/adversarial-eval.md) and
[the debt sweep](docs/debt-sweep.md) each keep their decisions in
strict modules under `schedules/plans/` and their effects in shells under
`schedules/jobs/`.

### How built-ins get created

Built-in provisioning (`base/daemon/schedules/builtin_schedules.py`) creates every
manifest schedule missing from the `schedules` table, with `enabled` taken
from the manifest's `default_enabled`. Schedules with `placement: "gateway"` (the default) are assigned
to the gateway machine. Schedules with `placement: "per_machine"` (such as `harness-cli-update`) are provisioned
for each machine in `machines` carrying the `agent-runner` role as `<name>:<machine>`. It runs:

1. **at gateway startup lifespan** (before serving traffic) — gated by
   `settings.gateway.provision_builtin_schedules`, and
2. **`meiva schedules provision`** — manual restore, works even while the
   gateway is down.

Provisioning is **idempotent**. A missing row is created. An existing row whose
script, command, trigger, timezone, or timeout differs from the template is
**resynced** to it: the DB copy
of a built-in is a snapshot made when the row was created, and a snapshot goes
stale the moment a library function the script calls changes shape (2026-10-03:
`catch_up()` gained a required `db`, 13 stored copies crash-looped). The resync
writes a `schedule_versions` row (`builtin-resync <hash>`, so the replaced text stays
recoverable) and queues a sync request. When the old row is a `daemon`, that
request reaps `meiva-schedule-<id>` before the declarative engine may admit a
round. `enabled` and `description` of an existing row are never touched, so
`meiva schedules stop <name>` (or disable in the UI) keeps a built-in around without
running. Delete a built-in and the next provision brings it back with its manifest
default. The checkout is the source of truth for a built-in's code: a hand edit of
its stored script is replaced at the next provision; edit the template instead.
Schedules that are not in the manifest (agent-created ones) are never read or
written by provisioning. Rationale and rejected alternatives:
[decision](../docs/decisions/agents/scheduling/2026-10-03-builtin-schedules-resync-from-repo.md).

### Adding or changing a built-in

Edit the script template and the manifest entry (name, class, default_enabled,
description, trigger and timeout), PR it, and deploy: the next gateway startup
syncs it into existing clusters.

Four places change together; each one left out fails a directory-wide gate
rather than a review:

1. **The template.** `tests/components/base/test_schedule_timezone.py` sweeps
   every `*-schedule.py`. Timezone-sensitive work reads `cluster_timezone()`;
   the cron expression and canonical timezone policy live in the manifest.
   Jobs that branch by slot read `MEIVA_SCHEDULED_FOR`, not the host clock.
2. **`meizel.toml`'s `[uses]`.** An import of a sibling directory (a template
   imports `schedules/jobs` for the entry and agent calls, and
   `schedules/plans` for the decisions) needs its declared edge, or
   `meizel check` fails the locality gate.
3. **The manifest entry and this table** — name, class, `default_enabled`,
   description.
4. **`base/daemon/schedules/tests/test_builtin_schedules.py`**, whose
   repo-manifest test names every entry's class and the set of schedules that
   start by default. A new entry cannot pass by carrying the right class and the
   wrong default.

A template is thin: its `__main__` block reads the scheduled slot, timezone, and
operator inputs once, builds the values its job needs, performs one round, and exits.
What a schedule decides lives in a strict pure module
under `schedules/plans/`, and what it does lives in a shell under `schedules/jobs/`;
[declarative rounds and the agent directory](docs/loop-and-directory.md) say how. The
C9 job imports its accounting dependency through `scripts.ci.pull_requests.accounting`
where it runs. Materialization changes the script directory, not the installed module
roots (`schedules/tests/test_scripts_entry.py` runs every template from a copy). Tests
live in `schedules/tests/`.

A `.py` template is executed in-process by the runner, which hands it a clean
`sys.argv` — just its own path, exactly what `python <script>` would give it; the
runner's own argv (`python -m services.wake.schedule_manager.runner <id>`) never reaches the
script, so a template's CLI takes its own flags only (keep them usable for
manual runs). On deploy, new manifest entries are
provisioned and changed templates are resynced at the next gateway startup;
changed `default_enabled` / description only apply to rows that do not
exist yet — an existing cluster's rows keep their operator-set state. To push a
template into a running cluster without a restart of the manager, run
`meiva schedules provision`.

## Machine placement & Multi-machine operation

Every schedule row records a target `machine`, an optional `owner_plugin`, and an
optional owner agent. Declarative owner placement defaults to the agent's machine;
system and compatibility rows default to the gateway when no machine is explicit.

- **Per-machine schedule manager**: `schedule-manager` runs on all cluster nodes (`capabilities=Capabilities.BOTH`, `requires_db=True`, `db_access="runner"`). Each instance reconciles only rows where `machine = self._machine_name`.
- **Split-unit tie-breaker**: On hosts with multiple units sharing a machine name (such as preview environments), `is_active_unit()` inspects `machine_units` and activates the instance on the serving gateway unit (or the lowest lexical home path). Inactive units yield reconcile and sync loops.
- **Session reaping across moves and deletes**: Compatibility daemon moves and deletes reap the old PTY without overwriting the row's error outcome.
- **Machine migration**: Moving a daemon schedule enqueues old/new machine convergence. An idle declarative schedule keeps its durable next-fire state and launches on the newly assigned manager; machine moves and deletion wait until an active run finishes.
- **Placement status API**: The gateway evaluates placement health via `judge_placement()`:
  - `healthy`: machine is registered and reachable.
  - `unplaced`: schedule row has no machine assigned (`NULL`).
  - `unregistered`: assigned machine does not exist in the `machines` table.
  - `unreachable`: assigned machine has no active heartbeats or units.
  - `stalled`: machine is unreachable and the schedule is currently active or enabled.
- **Cross-machine log capture RPC**: Remote reads dispatch `schedule_capture` to `agent_ops`; declarative rows read owner-local `latest.log`, while daemon rows read live or saved PTY output.

## Deployment notes

- **The DB is authoritative.** The manager materializes each schedule's script
  from the `schedules` table to its system or owner-local schedule directory; editing
  only the on-disk copy is overwritten at the next launch/restart. Change a
  schedule through `meiva schedules update` / the API, never by editing the
  materialized file.
- **Cron expressions are cluster wall clock.** Provisioning resolves an omitted
  manifest timezone to `MEIVA_TIMEZONE`, so `0 4 * * *` means 04:00 cluster
  time on every host. The manager persists the resolved timezone and exports
  the selected UTC slot as `MEIVA_SCHEDULED_FOR`; the host OS timezone never
  enters the decision.
- **Report recipients are operator knobs.** A daily report resolves its
  recipient from its own `MEIVA_*_REPORT_AGENT` variable when set — a numeric
  agent id, and a malformed value stops the schedule at its start instead of falling
  back — and otherwise from the fixed report label the script declares
  (`_REPORT_LABEL`). The variable is read once, when the script starts.
  `MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT` defaults to agent 228, and an
  explicit empty value skips that report. The debt-clearing worker label comes
  from `MEIVA_DEBT_SWEEP_AGENT_LABEL`.
- **Each built-in slot has one run row and one process.** Admission commits a
  `schedule_runs` row before launch and refuses another open run for the same
  schedule. The process has no PTY, reads no stdin, writes `latest.log`, and is
  killed as a process group at its declared timeout. A failure records
  `last_error` and one failed run; recurring triggers advance once and do not
  retry the same historical slot in a storm.
- **Recovery catches up once, then resumes cadence.** An overdue recurring
  schedule fires its current `next_launch_at` once after manager recovery.
  `cron` and `every` then advance from `max(slot, now)`, skipping older history.
  Stateful watches keep their window in workspace `state.json`, so a process
  boundary does not reset alert streaks.
- **Compatibility loops remain for existing third-party daemon rows.** Their
  heartbeat and failure-backoff contracts remain in
  [`docs/loop-and-directory.md`](docs/loop-and-directory.md); shipped built-ins
  no longer allocate those resident PTYs.
- **A cron that stops arriving is its own card, and the card publishes nothing.**
  `release-gate.yml` is the only post-merge run that certifies the whole
  population on a fixed cadence, and this repository's GitHub crons do not keep
  it: in the three days to 2026-11-04 the six-hourly gate should have run twelve
  times and ran six, the latest slot starting 1h48m late. The watch reads the
  newest `event=schedule` run of that workflow each round
  (`schedules/plans/watch/release_gate_slot.py`). Past
  `RELEASE_GATE_SLOT_STALE_AFTER` (nine hours), with nothing of that workflow
  running and commits uncertified, it opens one card. Nine hours is deliberately
  not a whole number of slots: across this repository's 164 scheduled runs,
  arrivals are late by 45m to 7h50m (median 4h34m), so the threshold is a
  judgement about arrivals, and the five most recent gaps (4h09m, 8h09m, 9h59m,
  8h49m, 13h21m) put nine hours between the slowest gap that was merely late and
  the fastest gap that really missed a slot. The round never dispatches: `release-gate.yml` certifies on success,
  so a dispatched run mints a `v*` tag and moves `channel/latest`, and a release
  is the user's decision. The card names `main-typecheck.yml` as a type-only
  coverage option that publishes nothing; such a dispatch does not restore the
  cron. The watch dispatches neither workflow. A slot the gate would have skipped
  anyway (nothing uncertified) opens no new card. An existing card closes only
  when a strictly newer scheduled run appears, whether that run is red, green or
  still busy. Manual/PR activity and channel progress keep an existing gap waiting.
  The schedule-only listing verifies the anchored 24-hour window; empty, capped
  or incomplete reads preserve the card as unreadable. The card also states how many
  scheduled arrivals the last 24 hours should have contained and how many it found
  (`_arrivals`), because that count is the one number a reader can check against
  the workflow's own run list: one card is then judgeable on its own, without
  waiting for an interval that happens to look suspicious.
- **The slot leg fails alone.** Its two reads -- the cron's own starts and whether
  the workflow is busy -- run inside `_slot_note`, not in the argument list of
  `slot_round`, so one bad answer becomes one note on the round line instead of
  an exception that leaves `fire` (which nothing catches) and takes the round's
  heartbeat with it. On a failed read the leg is skipped rather than run with
  `last_schedule_start=None`: that value means "never started", and a card built
  from it would report a stopped cron on the strength of a failed read.
  Skipping is a degradation, not silence: `SlotLedger.unreadable_rounds` counts
  the consecutive rounds it lasted, the round line names it every third round
  ("nothing is watching the slot while this lasts"), and the first readable round
  afterwards says how long it was dark.
- **A silent nightly says what to run.** The liveness note for `ci.yml` ends with
  the dispatch that closes the gap -- `gh workflow run ci.yml --ref main`, the full
  suite, which publishes nothing and takes about ten to fifteen minutes -- because
  the stream carries `make_up=` in `schedules/jobs/watch/main-red-watch-schedule.py`.
  A stream whose dispatch cost nobody has timed carries no `make_up` and reports
  the silence alone, rather than naming a command with an invented price.
- **A template change reaches a running cluster at the next gateway startup**
  (see "How built-ins get created"), or at once through
  `meiva schedules provision`. A resynced declarative schedule uses the new
  script on its next fire; the sync request first removes any legacy PTY. A
  disabled row keeps its operator-selected state. `meiva schedules get <name>`
  shows which script the row holds. A
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
  (`inputs.database()`, job entry calls;
  the argument shape only, so a call it cannot bind statically — `*args`, an
  instance method, a rebound name, the plugin-wrapped `meiva.*` SDK — is skipped,
  never guessed). Nothing is started, stopped, or written. The drift classes that
  bit (a module move — e.g. the watcher module's move to `base.daemon.schedules.watcher`
  — leaving stale in-store imports, task #4800; a `shared` -> `base` rename leaving a
  top-level statement reading a name nothing imports — 2026-10-01 audit, three of 16
  copies; a changed signature — `catch_up()` gaining a required `db`, 2026-10-03 —
  leaving calls that raise `TypeError` on the first fire) are caught by its
  `RED id=<id> name=<name> missing=<module|compile-error:<l>:<m>|undefined-name:<l> <name>|call-signature:<l> <call>(): <why>|...>`
  lines before they can fire. `meiva update down` runs it with the NEW
  checkout's code before stopping anything, and `up` runs it again on the gateway. Exit codes: 0 clean / 1 red / 2 tool error. Run it on the
  host that runs the schedules, or from a dev worktree to check the same table
  against in-development code. `--check-file PATH` checks one script file
  instead of the table (the falsification hook); `--rows-file PATH` sweeps a
  JSON dump of the table (`[[id, name, script], ...]`) with no database access and
  no signal; a non-clean sweep of the live table emits one `schedule_verify_failed`
  event for the Grafana rule to read (`--no-notify` suppresses that for a dry run).
- Deploy a one-off system schedule with:

```bash
meiva schedules create --name <name> --at <iso-instant> \
  --script-file <script.py> [--description "..."]
```

The materialized copy lives under the schedule's system or agent workspace. The
database remains authoritative; never edit only the running copy.

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

Compatibility daemon templates receive immutable `ScheduleInputs` through
`runpy.init_globals`:
one loaded image, the entry's database/pipeline builders and its `MachineIdentity`.
A template's `__main__` hands the injected inputs (or `None` when run by hand) to
`open_schedule_entry(inputs, home=...)`: injected inputs are borrowed without closing them or changing
the shared SDK context, and a hand run owns a fresh entry built by
`fresh_schedule_entry(image=..., process=..., home=...)` for the commit it loaded and the home the
script's entry resolved, with its lazy
machine, SDK context, gate and cold writer. Exit restores the prior SDK context and
closes constructed clients within two seconds while preserving the original failure.
Declarative entries open the same context from their process environment; this
binds `schedule:<id>` and an optional owner agent for SDK attribution. C9 and
the debt sweep open process logging and the event writer only when they report
(`schedules/jobs/telemetry_event.py`); debt dry runs assemble no root.
