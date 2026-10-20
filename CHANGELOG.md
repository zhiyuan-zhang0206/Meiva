# Changelog

Notable changes, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Meiva is at 1.0.0; the
per-release PR-level detail lives in the annotated release tags (`git tag -n99`)
and the matching GitHub Releases.

## [Unreleased]

### Added
- `meiva stop --maintenance-for 45m --reason "..."` declares how long a host is expected to be
  down: the stop records `machines.maintenance_until` / `maintenance_reason` on the host, and the
  next start clears them. An ordinary stop clears whatever a previous one declared, a
  co-located peer still serving leaves the host without a window, and `stopped_at` keeps the
  first stop's clock instead of moving forward on every recompute (task #5464).
- A host that announced an intentional stop and is still down 20 minutes later with no window
  ahead now reports itself: the gateway liveness pass emits `machine_stopped_too_long` per pass
  (`stopped_for_s` carries the age), and the `meiva-ops-machine-stopped-too-long` rule turns it
  into an error per machine. An intentionally stopped host is out of the probe target set, so
  the 2026-10-14 stack sat stopped for 53 minutes with nothing reporting it; a paused or staging
  host and a host that never announced a stop stay silent. A whole-stack stop is still the
  cluster-external probe's job, not this rule's (task #5464).

### Removed

- A message card in the Messages timeline no longer offers a fork action on hover (user
  ruling 2026-10-20): the card's hover pill carries Copy alone, and `TimelineView`'s
  `onFork`/`forkPending` props, the row plumbing behind them and the `timeline.forkFromHere`
  label are gone. Forking starts from an agent row's context menu in the sidebar, which keeps
  its quick fork and fork-with-prompt entries
  ([card chrome](ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md)).
- The maintenance hold is gone: `base/deploy/maintenance/`, `ops/agent_pause/`, the
  `run/deploy-pause-owner.json` journal, the stop's drain/quiesce phases, the 503 on every
  business route while a unit is stopping, the hold gates of the host, the schedule manager
  and the ops server, the `--json` flag of `meiva status` (its only payload was the hold) and the
  `maintenance_hold_overdue` / `agent_continuation_lost` events with their alert rules. A stop
  clears the serving gate and stops the services in root's order; the agent host goes first
  and settles running turns on its own SIGTERM exit, and the ops server drains the requests
  it admitted before it exits. The fleet update judges a stop by `run/lifecycle-op.json`
  and a start by `meiva start`'s exit code; a stored schedule script is rewritten only while
  no agent host holds its instance lock. The `maintenance_hold_alert_after_s` and
  `pause_lifecycle_wait_seconds` settings and the `maintenance` value of
  `GET /api/cluster/status`'s `paused_reason` are removed
  ([decision](docs/decisions/runtime/processes/2026-10-20-delete-maintenance-hold.md)).
- The `host_deploy_state` per-host posture table is dropped (migration
  `20261020T151143_drop-host-deploy-state`) with `base/deploy/state/` and
  `ops/cluster_status/pause.py`: neither `meiva start` nor `meiva stop` writes a posture
  any more. `paused` on `GET /api/cluster/status` and the roster means the serving gate has
  not reached `serving` or the host's database snapshot failed, and its `paused_reason`
  loses `business_pause` (`no_state` and `startup` remain). A pure gateway machine reads the
  same serving gate
  ([decision](docs/decisions/runtime/updates/release/2026-10-20-delete-host-deploy-state.md)).
- The frontend pixel-regression apparatus is gone (user ruling 2026-10-20): the
  Visual baselines workflow, the screenshot regression suite with its goldens, the
  preview-gate matrix with its 44 captures, and the post-deploy visual gate. The
  real-browser structural probes it shared (overflow, containment, occlusion,
  visibility) remain, unused by any pixel comparison.

### Fixed
- The gap between timeline cards no longer depends on which kinds they are: the
  timestamp and Copy took a line under the card, and only the kinds that stamp a
  timestamp or offer Copy got that line, so two plain cards sat 12px apart and two
  stamped ones 28px. The footer now floats in the card's own bottom-right corner
  and takes no height, the stamp drops its brackets and renders one step smaller,
  and Copy's icon shrinks with it (user report 2026-10-21).
- The Telegram notice bridge honors the user's per-category switches: a decision
  notice under a switched-off `core.needs_input` is accepted (the poll cursor still
  advances) but not pushed, so "turn notifications off" now means the same thing on
  every transport. Its ambient FYI feed carries no notification category and is
  unchanged ([notification spine](base/events/notifications/docs/notifications.meiva.okf.md)).
- Collapsing or expanding the sidebar no longer tears the conversation down and
  rebuilds it. The columns group used to be swapped for a bare div, so the main
  subtree unmounted on every toggle: the list's height flashed and the reader's
  place came back from the per-entry bookmark rather than staying put. The group
  now stays mounted and only the sidebar's own panel and divider come and go,
  and a width change re-applies the bookmark's anchor, so a mid-history reader
  stays on the row they were reading (user report 2026-10-20).
- The scroll-to-bottom button's loading ring is the same 20px ring the pull-to-load
  gesture draws, from one component, instead of a second circle drawn to the
  button's own edges: at r=16.5 in a 36-unit box it read as a dial, not a loader
  (user report 2026-10-20).
- The inline connection banner is translated and says what the reader loses rather
  than which transport dropped: "Live updates reconnecting…" / "Live updates
  disconnected — refresh the page to restore them", from the `common` catalog
  instead of hardcoded English, so a zh console is no longer half-translated.
- A delivered notification now names the notice it is about: the spine stages the
  notice's own route in `href` (`/fleet?notice=<id>`), and the app's bridge opens that
  target on a tap instead of a hardcoded `fleet#inbox` — which on a wide window only
  switched a mobile tab, so the user landed nowhere near the row the ping was about.
  Only a same-origin path from the delivery is accepted; `fleet#inbox` stays the
  fallback ([notification spine](base/events/notifications/docs/notifications.meiva.okf.md),
  [app delivery](ui/app/docs/notifications.meiva.okf.md)).
- An Inbox row states its obligation rung (Decision / FYI, with Blocking on the stalled
  ones). The list rendered only the blocking flag, so a P2 decision and a P2 FYI looked
  identical and the queue could not say which rows actually wait on the user
  ([Fleet view](ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md)).
- The Inbox tab badge counts the panel's own feed (`useNotices()`), not a second roll-up
  of the agent snapshot: a terminated owner's open notice was counted by the list and not
  by the badge, so the tab said "Inbox 4" over a list of two
  ([Fleet view](ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md)).
- Switching agents no longer scrolls a conversation back up. A reader on the tail
  was saved as a pixel bookmark of one row, so the return restored that row's old
  offset — above the newest message by everything that arrived meanwhile, and
  still there once the thread stopped streaming. A reader inside the bottom zone
  is now saved as following the bottom and returns to the newest message; a
  reader above it still returns to the item they were reading
  ([timeline view](ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md)).

### Added
- A navigation now reports its own timing from the browser that felt it: click to URL change,
  click to first frame after it, and the longest main-thread task inside that window, as one
  `nav-timing` telemetry event per route change. It exists because a report of "clicking Settings
  took ten seconds" could not be reproduced in a fresh tab (100–250 ms there), so the numbers
  have to come from the reporter's session
  ([frontend state](ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md)).

### Changed
- `meiva stop` and `meiva restart` call shell sessions shell sessions, matching
  `meiva.shell.sessions` and the `--keep-shell-sessions` / `--close-shell-sessions` flags, and no
  longer call them terminals. The stop journal's phase and failing stage are `shell-sessions`
  (was `terminals`), a surviving shell's `role` in the journal's survivor inventory is `shell` (was
  `terminal`), the `[meiva stop]` plan line prints `shell_sessions=retained|closed`, and the
  closure notice an owner agent reads says "shell-session closure". A journal an older build wrote
  keeps its old names; nothing reads the stage or role back. The stop kernel's helpers are renamed to match
  (`close_shell_sessions`, `force_close_shell_sessions`, `require_no_shell_sessions`,
  `live_shell_sessions`, `await_no_shell_sessions`); the `pty-sessions` service, its socket and
  `base/sessions/pty/` keep their names.
- Which harness runs which models is the model registry's declaration, not a frontend table
  (user ruling 2026-10-20): `ModelSpec.external_harnesses` names the external harnesses that can
  run a model, `GET /api/models` publishes it per model as `harnesses` (built-in runtime first,
  always present), and the spawn form filters its model column by that field instead of the
  deleted `HARNESS_PROVIDERS` map. `claude-*` declares `claude_code`, `gpt-*` `codex`,
  `gemini-*` `antigravity`; a value outside the `Harness` enum is refused at catalog build
  ([harness identity](base/agents/docs/harness/harness.meiva.okf.md)).
- Opening Control no longer freezes the page: the section bodies, and the config
  form's seventeen groups inside them, queue up and mount one per idle slice
  instead of all in one commit. The page still lands fully populated (7874 nodes,
  116 switches) and Ctrl-F still reaches every field; only the scheduling moved.
  Measured on the deployed preview at 8x CPU throttle: main-thread blocking after
  the click 1208 ms -> 505 ms, longest single task 643 ms -> 166 ms
  ([Control first paint](ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md)).
- A force graph of 200+ nodes settles in time-budgeted slices instead of one React render per
  physics tick: every live tick committed a fresh position map, and every commit repainted the
  whole SVG, so the preview's 1,949-node Fleet graph spent 4.4 s of main-thread time in its first
  six seconds at 4x CPU throttle (profile: native layout/style/paint, not JavaScript). Smaller
  graphs keep the live settle
  ([Fleet view](ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md)).
- The scroll-to-bottom button's ring is the tail read that follows a switch or a
  reconnect (`loadingNewer`), not any in-flight fetch: a background refetch no
  longer spins it for nothing, and it can no longer overlap the pull strip's ring
  for an older-history load. The pull ring scales
  with the revealed strip instead of being sliced into a bar by its clipping.
  The create popover's footer says "Spawn" when there is a single spawnable
  host — the machine name is a deployment detail, not a label
  ([sticky scroll](ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md),
  [creation availability](ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md)).
- A message card's timestamp and Copy moved out of the card into a footer under it, aligned to
  the card's bottom-right and revealed on hover or focus (user ruling 2026-10-20): the card box
  keeps its own padding, the footer reserves its own line so nothing shifts when it appears, and a
  coarse pointer — which cannot hover — keeps it visible
  ([card chrome](ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md)).
- The create popover is one table instead of three dropdowns: harness, then the models that
  harness runs, then the efforts that model publishes — each column narrows the next and the
  config the last spawn used is marked on entry. A click is only a pick until a spawn is issued,
  so closing the form without spawning leaves `behavior.spawn_*` untouched, and the model row
  keeps the input price visible with every rate and the vendor TPS record on hover
  ([creation availability](ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md)).
  A collapsed right panel's reopen control is pinned to the header bar's right edge instead of
  riding to the middle of a wide viewport with the centred conversation column
  ([home shell chrome](ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md)).
- A restart interrupts the model call or code run its turn is in once it has waited 10
  seconds (cancel and terminate still interrupt at once), and a restart that ends an
  unfinished turn resumes it: the transaction that executes the restart queues a pending
  `lifecycle_restart` system note, and the agent continues from its checkpoint when the next
  wake claims it. An interrupted code run returns its output so far marked
  `[interrupted by restart]`; a half-generated reply is dropped. External restarts, the
  maintenance hold's, the SIGTERM graceful exit's and the agent's own `meiva.self.restart()`
  resume alike; an idle agent only reads the acceptance marker, and a hard kill resumes
  nothing. This replaces the lifecycle pointer the graceful exit relied on to wake the next
  host
  ([decision](docs/decisions/runtime/processes/2026-10-20-restart-interrupts-after-a-grace-and-resumes-by-a-note.md)).
- An operator `meiva stop` / `meiva start` / `meiva restart` now records how it ended,
  not only that it was asked: `lifecycle_result` carries the verb, the units, `outcome`
  (`ok`/`failed`), the seconds the command held and the same `head_sha` the request
  named. A request with no result is a command that died mid-flight, and a stop followed
  by a start with the same `head_sha` is a same-code restart — two facts that took log
  archaeology to establish for the 2026-10-14 outage (task #5464).
- The gateway `/mcp` endpoint is a thin forwarder onto the HTTP API (user ruling 2026-10-20):
  it authenticates like any API route (cluster bearer, session or `trusted_network`), each
  tool is one in-process request to its API route, and an API error becomes the tool error.
  MCP client tokens, their `read`/`write` scopes, the `/api/mcp/clients` routes, the
  `mcp_clients` tables and the `mcp_tool_call` audit event are removed; `send_message` takes a `source` and
  `spawn_agent_guarded_v1` a `prompt_source` and `spawner` that the API validates; `get_messages` returns
  the API's raw message window
  ([decision](docs/decisions/data/security/2026-10-20-mcp-is-a-thin-api-forwarder.md)).
- The agent host no longer fences itself: one host per machine and home is the protection. A turn
  is admitted by breaker, maintenance hold and one `UPDATE` of the machine's own row; the host
  settles its `running` rows once at start. Removed: the per-agent lease and its renewal, exec
  resource evidence and closed-predecessor admission, admission refusal records and their event,
  the resurrection epoch, the legacy cutover gates, cold maintenance preparation, and the columns
  and tables behind them (`pid`, `started_at`, `lease_expires_at`, `runtime_kind`,
  `incarnation_resources`, `last_admission_outcome`, the lifecycle pointer and the keyed receipt
  tables). `pid`, `started_at` and `runtime_lease_expires_at` leave the agent API and
  `meiva.agents.AgentRow`; liveness follows machine reachability only. A held maintenance drain is
  continued by a restarted host without manual cancellation.
- Restart and terminate commands are claimed at the turn boundary and executed and closed in
  one transaction; a force terminate writes `terminated` in the API's own transaction and then
  asks the home host to cancel the running turn. The pointer, target, applied and observed
  stages, the force fence and the stuck-pointer scan and its alert are gone. The stop button,
  `meiva agents cancel` and `meiva.agents.cancel` call one plain route,
  `POST /api/agents/{id}/cancel` (`cancelled` / `not_running`), instead of the keyed native-work
  pair. `meiva agents compact`, the compact action and `meiva.agents.compact` queue a
  `compact_request` through `POST /api/agents/{id}/compact`. Removed: the keyed
  `native-work`, `cancel-work`, `restart-work`, `restart-commands`, `compact-target`,
  `compact-history` and `compact-commands` routes and `meiva.agents.work` and
  `meiva.agents.compaction`.
- A notification is only a request that needs the user's reply (user ruling 2026-10-19).
  `core.turn_finished` and `meiva_fleet.task_closed` are gone as categories and producers:
  the agent host logs a finished turn and the task registry logs a closed task, both
  already queryable through the settled agent row, the task board and the `task_updated`
  event. `core.needs_input` is the only category; a plugin category always ships switched
  off (`defaultEnabled: true` is a manifest error), the async delivery path that existed
  only for the turn host is removed, and `meiva notices audit` replays the notice producer
  alone. Guard tests fail if any category other than `core.needs_input` ever ships enabled
  ([notification spine](base/events/notifications/docs/notifications.meiva.okf.md)).
- `run_background` and the watcher entries (`launch`, `at`, `cron`) return one
  `SessionHandle(session_id, output_path)`: a watcher's log path is known at launch, so the
  handle carries it instead of a bare session id. `sessions.send`, `send_keys`, `capture`,
  `renew` and `kill` accept the handle or its `session_id`, so `sessions.kill(watcher.launch(...))`
  and stored ids keep working ([meiva-shell](meiva/shell/docs/shell.meiva.okf.md)).
- Root's health monitor spaces consecutive replacements of a DOWN unit by one backoff ladder
  (60s, 5m, then 15m) instead of restarting it every round, and opens a per-unit circuit breaker
  when the restart budget is spent. While a breaker is open, one bounded `SELECT 1` per round tells
  the monitor when the cluster database answers again; the first usable observation and every
  unavailable-to-answering edge after it grant one immediate attempt. After 2026-10-18's database
  stall the four machines' agent hosts stayed down for 36 minutes because nothing retried them and
  nothing was reported. `root_unit_breaker_open` /
  `root_unit_breaker_closed` mark the transitions (attempts, last error, time held), the per-round
  `root_unit_failure_state` carries `attempts` and `last_error`, both root-unit alert rules group by
  machine as well as unit, and the breaker rule also goes straight to Telegram — the webhook's
  ingest writes its row to Postgres, so a database outage silences that path
  ([meiva-root](services/supervision/meiva_root/docs/meiva_root.meiva.okf.md)).
- A cluster update keeps the stateful components its window does not touch
  ([decision](docs/decisions/runtime/updates/release/2026-10-17-component-scoped-keep-on-update.md)):
  `cli.fleet_update down` derives a dependency closure over the window's `OLD..NEW` diff
  (`scripts/host_ops/fleet_update_keep.py`, the `meizel` facts the locality gate cites) and adds
  `--keep-service pty-sessions` and/or `--keep-service browser --keep-service browser-mcp` to every
  stop, so live agent shells stay mounted and the shared Chrome keeps its sessions when their code
  did not change. A touched component — or any decision failure — restarts as before, and
  `--restart-all` skips the decision.
- An external agent (Claude Code, Codex, an MCP client) can message an Meiva agent
  through `meiva agents send --source ...`, the SDK or MCP `send_message` in two
  ways: `user` delivers as the user, and `external_agent:<subject>[:<instance>]`
  delivers as a message from that declared agent (shown with its name in the
  agent's context and in the web timeline). The runtime-protocol negotiation
  that refused every declared external agent is gone; MCP `send_message` takes
  a `source` argument (default `user`), validated by the API, in place of `caller_protocol`.
- Alert noise from the 2026-10-02/03 triage is downgraded to what each cause
  earns: private-storage convergence skips are one summary WARNING per tree
  (per-node DEBUG, `node_modules` no longer descended — the 18,516-line/24h
  source), `host_turn_reconcile_skipped` and split-runner `direct_db_url` log
  INFO, `sse_drop` splits transport losses (INFO) from queue-full (WARNING), a
  dispatcher reconnect, the first Telegram push failure and a page_server
  relaunch log WARNING without a traceback, the interrupt watcher's
  abandoned-task report and psycopg's `query cancellation failed` line log at
  INFO (event-side only), and a browser-UA gateway 401 is DEBUG — post-rollout
  tab retries, with the `auth401_rejected` aggregate still counting every
  rejection. `delivery_stalled` is the single stall signal (its log line is
  INFO) and the agent db pool reports a slow borrow after
  `MEIVA_DB_POOL_SLOW_ACQUIRE_WARN_SECONDS` (default 10s, was 3s).
- Alert notification is Grafana Alerting's alone
  ([decision](docs/decisions/observability/alerts/2026-10-04-alerting-on-grafana-alerting.md)): signal ->
  rule (`for:` is the debounce) -> notification policy (grouping, repeat) ->
  the webhook into the gateway's `POST /api/alerts` ingest -> IM. The health
  probe, the machine/station liveness pass, the exec boot failure, the
  inspector-metrics gap, the impersonation event log, the root unit episodes
  and the IM push watchdog no longer write alert rows or push IM themselves;
  each emits a declared event and a rule in `rules.yml` carries its alert. The
  in-code gates are gone with their keys: `MEIVA_ALERTS_TRANSITION_WARNING_SECONDS`,
  `MEIVA_ALERTS_TRANSITION_ERROR_SECONDS`,
  `MEIVA_ALERTS_INSPECT_METRICS_DEGRADED_COOLDOWN_SECONDS`,
  `MEIVA_DELIVERY_WATCHDOG_ALERT_GRACE_SECONDS`,
  `MEIVA_DB_POOL_SLOW_ACQUIRE_WARN_COOLDOWN_SECONDS`,
  `MEIVA_VENV_PROBE_FAILURE_THRESHOLD`, `MEIVA_BREW_PIN_PROBE_FAILURE_THRESHOLD` and
  `MEIVA_BROWSER_REACH_FAILURE_THRESHOLD`, the root-unit episode store,
  `terminal_escalate_rounds`, the boot-defer streak ledger (`hosted_boot_recovery_stalled`
  is now `hosted_boot_recovery_deferred`, one event per deferred boot) and the
  deploy-window gates of the delivery watchdog and the shell reaper.
  `python -m cli.fleet_update down` opens one Grafana silence over every alert
  (the disk alerts excepted) and `up` expires it; `meiva cluster health-probe`
  emits `health_probe_ran` every run, and its absence is itself a rule.
- Agent shells live in one `pty-sessions` roster service per machine instead of one detached
  host process each. A session outlives an agent, an agent host and a gateway restarting; `meiva
  stop`, `meiva restart`, an update, a service crash and a reboot end every session, and `meiva stop`
  and `meiva restart` notify the owners of busy ones. A shell's base environment is the service's
  plus the forwarded env, so a variable only the creating process held is not carried over
  ([decision](docs/decisions/runtime/processes/sessions/2026-10-03-pty-sessions-service.md)).
- Owners of busy shells are also told when a `pty-sessions` service dies uncleanly (a crash, a
  SIGKILL, a reboot): the next service start, or an `meiva stop` that finds no service, writes a
  notice for each session the ledger last saw running a job. A database that cannot be reached
  only logs
  ([decision](docs/decisions/agents/messages/2026-10-04-pty-crash-notices.md)).

- `meiva start` settles a maintenance hold's failed continuation receipts instead of refusing
  to run: once the unit serves, each failed agent's restart pointer is re-delivered with the
  hold's release, an agent whose pointer is gone is logged at ERROR and its owner told (alert
  row plus IM push), and the hold releases. `meiva start` is also the exit for a hold that never
  stopped services. `meiva status` prints the maintenance hold, and its `--json` flag prints
  only it as one JSON object (`{"hold": {...}}`)
  ([decision](docs/decisions/agents/graph/2026-10-04-delete-meiva-maintenance.md)).

### Removed
- `meiva maintenance` in full (`status`, `repair`, `cancel`), with `repair`'s operator-identity
  record in the hold journal (`repaired`, `repair_record`; a journal that carries them still
  reads) and the agent-host absence probe only it used
  ([decision](docs/decisions/agents/graph/2026-10-04-delete-meiva-maintenance.md)).
- Zero-effect CLI surfaces: `restart --force-reap` (identical to `--mode force`), the
  unreachable `boot` lite-verbs entry, the `mcp ls` / `plugins ls` aliases, the no-op
  `stop --stop-browser`, and `cluster health-probe`'s `--agent-min` /
  `--crash-loop-max-restarts` / `--crash-loop-window-minutes` plus
  `health-probe-register --interval` — the defaults are the contract and the config keys
  stay; the flags now fail at argument parsing
  ([decision](docs/decisions/engineering/design/simplification/2026-10-03-deletion-sweep.md)).
- Four read endpoints with no consumer: `GET /api/agents/{id}/activity` (its writer went
  with `meiva.self.log` in 2026-08-02; the `agent_activity` table drops separately — the
  "remain (history)" note in the notice-SDK-slimming decision is overturned),
  `GET /api/agents/{id}/completion-notice-policy`, and `GET /api/notices/escalations` /
  `GET /api/notices/resolved` (the unified `GET /api/notices` feed carries the resolved
  page) ([decision](docs/decisions/engineering/design/simplification/2026-10-03-deletion-sweep.md)).
- The Loki read side in `base/telemetry/loki_index_labels.py` (`event_stream_selector`,
  `archive_stream_selector`, `split_index_label_window`, `ledger_gap_plan`,
  `retention_floor`, the read-era/slice/plan types) and `INDEX_LABEL_CUTOVER_AT`: the last
  Loki event readers went in the telemetry-readers-on-postgres move
  ([decision](docs/decisions/engineering/design/simplification/2026-10-03-deletion-sweep.md)).
- The mcp-daemon pre-rename compatibility (the `meiva._mcps_daemon` argv arm): a
  four-machine process census shows no pre-rename process left
  ([decision](docs/decisions/engineering/design/simplification/2026-10-03-deletion-sweep.md)).
- The `MEIVA_RUNNER_MODE` setting in the visual-baselines workflow and the lazy-import
  tests' clean-env strips: nothing reads it
  ([decision](docs/decisions/engineering/design/simplification/2026-10-03-deletion-sweep.md)).
- `meiva maintenance prepare`, `drain`, `stop`, `start`, `resume` and `stop-data-plane`, with
  `--keep-terminals` and `--gateway-last`: the same kernel as `meiva stop` and `meiva start`, taken
  one step at a time, with no caller. `meiva maintenance` kept `status`, `repair` and `cancel`
  (formerly `resume --cancel`) until it was deleted whole
  ([decision](docs/decisions/runtime/updates/execution/converge/2026-10-03-delete-manual-maintenance-verbs.md)).
- The per-session pty hosts with their records, orphan reaper, envfile handoff and CLI transport,
  `meiva restart` no longer keeps shells; the generic `--keep-service pty-sessions` does.
- `meiva pause`: a stop that kept the data plane, browser, helper and persistent shells.
  `meiva stop --keep-infra --keep-service NAME` names the retained services, and `meiva
  restart` keeps the data plane, browser and helper as before
  ([decision](docs/decisions/agents/graph/2026-10-03-delete-meiva-pause.md)).
- Port-block allocation and the start-time port scan and `.env` drift check:
  every home records one fixed port table (25 slots,
  `base/host/env/port_table.py`) at birth, and the health-port-base start option
  and the WSL default base are gone. The retired `restarter` and `coordinator`
  slots are dropped from the table, so a `start-intent.json` whose `record.ports`
  still carries them is refused at start until the two keys are deleted by hand.
- The retained-image release path (image preparation, the image-exec handoff, the
  finite executor, the fleet coordinator, write-generation rotation by rollout,
  PITR activation and their CI workflows) and the `shared/` release-probe shell.
  A cluster is updated from source with `python -m cli.fleet_update`
  ([decision](docs/decisions/runtime/updates/release/2026-09-30-remove-release-image-path.md)).
- The update straggler reap: a drain that truncated and released an agent still
  in a long turn, with its `reaped` hold receipts, settle-at-boot, four events
  and the `MEIVA_UPDATE_STRAGGLER_REAP_SECONDS` and
  `MEIVA_UPDATE_QUIESCE_TIMEOUT_SECONDS` settings. A stop still never kills a
  straggler; it waits out `--timeout` and the operator escalates with `--force`
  ([decision](docs/decisions/runtime/updates/recovery/2026-09-30-remove-straggler-reap.md)).
- The managed-writer publication fence (`base/deploy/writers/`): hosted admission
  no longer locks `deployment_state` or defers a birth on its phase, and always
  advertises protocol zero ([decision](docs/decisions/runtime/updates/publication/2026-09-30-remove-publication.md)).
- The cluster deploy lease (`base.deploy.state.cluster_lock`), `meiva cluster
  recover`, the roster's `deploy_hold` field and banner, and the lease readers in
  the deploy window, heartbeat, log sink, package refresh and `meiva stop`. A
  stranded pause is read with `meiva maintenance status` and ended with
  `meiva maintenance cancel` or `repair`; an unreadable journal is removed
  by hand ([decision](docs/decisions/runtime/updates/release/2026-09-30-remove-deployment-lease.md)).

### Fixed
- A codex session no longer hands its child the cluster's API key. The codex CLI
  prefers `OPENAI_API_KEY` found in its environment over the ChatGPT login in
  `~/.codex/auth.json`, so a key pinned for the cluster silently moved every codex
  agent onto per-token billing with no plan quota. `base/lm/harness/codex/auth.py`
  removes `OPENAI_API_KEY`, `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` from the
  environment the transport spawns with — removed rather than blanked, because the
  CLI asks whether the variable is present — and `CodexConfig.api_key_mode` is the
  one explicit opt-in that keeps them
  ([codex bridge](base/lm/harness/codex/docs/codex.meiva.okf.md)).

### Changed
- `lint_meiva_okf` no longer reports `[[wikilinks]]` quoted inside inline code spans or
  fenced blocks (the same code-sample exemption `check_doc_references` applies): a sample
  documents the syntax, it is not a node-graph edge.
- The venv diagnostic probe reports only after
  `MEIVA_VENV_PROBE_FAILURE_THRESHOLD` consecutive failing rounds (default 2)
  instead of the first: a single 5s subprocess deadline miss under high churn
  is not damage evidence, and the episode stays quiet until a healthy round
  re-arms it (task #4712).
- The bottom-layer package `shared` is now `base` (`base < meiva < agent <
  gateway < cli`), with no compatibility aliases: external plugins, schedules
  and skills that import `shared.*` must import `base.*`.
- The internal database plane always authenticates, whatever
  `MEIVA_CLUSTER_SECRET` says: `pg_hba` admits only the OS-user administrator by
  peer on the owner-only socket and SCRAM application logins; PgBouncer always
  uses SCRAM against the active write generation's verifier userlist (plus the
  `meiva_pooler_admin` console entry) and restarts when it changes. The schema
  owner is NOLOGIN, `MEIVA_DB_ADMIN_PASSWORD` and local `MEIVA_RUNNER_DB_PASSWORD`
  are retired, and `.env` holds a credential-free `MEIVA_DB_URL`. First start
  mints write generation 0 (`meiva_g0_gateway` / `meiva_g0_runner`, inheriting the
  NOLOGIN groups `meiva_gateway` / `meiva_runner`); the root launcher delivers each
  service its class login. A home born earlier (no ledger) is refused, and no
  conversion exists; `scripts/data_plane_ops/rotate_data_plane_secrets.py` now rotates Redis
  credentials only.

### Added
- The Ops dashboard gains a `Dev/CI` row backed by daily GitHub Actions run
  classification: per-PR duration and trigger percentiles, red/self-healed
  outcomes, fragile workflows, and deduplicated white-run classes. The
  watchdog remains explicit noise rather than inflating development failure
  signals (task #4014).
- The Ops dashboard gains two `Gateway & execution` panels for exec-envelope
  transfer cost (task #2174): envelope size (`size_bytes` p50/p95/max) and
  serialization cost (`serialize_ms` avg/p95/max), unwrapped from the
  `exec_envelope` event and grouped by envelope/op — the display split from
  the Exec outcomes other bucket (PM ruling 2026-08-31). A rising request
  band is state/payload inflation; a write-side serialize step is a
  dump-cost regression.
- `meiva impersonate send <session_id> --agent <agent_id> --to <target> --content '<text>'`
  — the attested CLI form of sending to another agent as a leased identity
  (previously reachable only through the SDK attachment); it delivers source
  `agent:<id>`, the same borrowed identity the attachment stamps (task #4102).
- The `impersonation_aborted` telemetry event fires when the native supervisor
  stops a takeover after a core-component death, carrying the agent, lease,
  session, the dead component (executor / relay) and the detail (task #3998).
- Grafana ops monitoring for LLM stream stalls (task #3889, wiring the
  #3884 stall telemetry): the Ops dashboard's LLM section gains a
  "Provider stalls (per minute)" panel — `stream_stalled_retry` sliced by
  vendor plus the `stream_stall_pair_terminated` pair count — and the
  `meiva-ops` alert group gains `meiva-ops-llm-stall-pair` (fires on the first
  two-adjacent-stall termination) and `meiva-ops-llm-stall-burst` (≥5 stalled
  streams per vendor per 15m; trailing-7d calibration: benign ≤2/15m, the
  2026-09-14/15 deepseek wave ran 2-9/15m). The pair event stays outside
  `LLM_ERROR_FAMILY` (it co-emits with the adjacent stall the family already
  counts); the new panel and rule are its display surface (task #3948).
- The cluster health probe gains two alert-only provider-account checks: a
  balance minimum (`MEIVA_PROVIDER_GUARD_BALANCE_MIN_CNY`, default 500 CNY)
  read from the provider's balance endpoint fires *before* the account runs
  dry, and a halted-agents check fires while at least
  `MEIVA_PROVIDER_GUARD_BLOCKED_AGENTS_MIN` agents sit halted by permanent
  provider rejections — both ride the existing 300s probe tick and edge-alert
  pipeline on the gateway host (task #3918).
- The watchdog gains a `browser-reach` check (agent-runner, browser-enabled
  hosts): a canary fetch through the shared Chrome — a throwaway background
  `about:blank` target closed in `finally` — contrasted with a same-process
  read of the gateway health URL, so "the browser's own network face cannot
  reach the gateway" (the 2026-09-18 stale-tab pool hang, task #3921) is
  detected instead of staying green. Report-only after
  `MEIVA_BROWSER_REACH_FAILURE_THRESHOLD` consecutive failing probes (one ERROR
  with both readings and the recovery pointer); throttled by
  `MEIVA_BROWSER_REACH_PROBE_INTERVAL_S`, bounded by `MEIVA_BROWSER_REACH_TIMEOUT_S`.
- Chrome pages created through the shared browser (an explicit `new_page`, or
  the auto-created page on a page-less first navigate) carry a hard TTL —
  `MEIVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`, 24h default — and the browser-mcp
  daemon's sweep closes them once the deadline passes; activity never extends
  it. A daemon-owned `renew_page` tool, appended after the upstream tool list,
  moves the caller's page deadline to now + ttl (at most 24h per call); a page
  already past its deadline is not renewable. Only pages this stack created
  are ever inspected or closed, expiry surfaces as the existing no-page path
  rather than an invented "expired page" error, and each expiry/renewal emits
  a `chrome_page_ttl_expired` / `chrome_page_ttl_renewed` event (task #3035).

### Changed
- Redis always authenticates, including a single box with an empty
  `MEIVA_CLUSTER_SECRET`: first start mints `MEIVA_REDIS_ADMIN_PASSWORD` (the
  `requirepass`) and the runtime ACL password in `MEIVA_REDIS_URL`, and no ACL user
  is ever created `nopass`. The passwords do not rotate per rollout. `meiva start`
  refuses a home born without them; re-birth it as a new home. The bearer
  still decides only Redis's network reach.
- The ops-facing `meiva` CLI moves its remaining argument checks to the parse
  layer (task #4092 batch B4, user ruling 2026-09-20): `schedules create`
  requires exactly one of `--script` / `--script-file`; `schedules update`
  and `presets update` require at least one field; `presets create|update
  --config` is validated as a JSON object at the boundary; `pitr drill`
  requires exactly one of `--chain` / `--candidate` and validates
  `--target-wall` as an offset-carrying ISO timestamp; `maintenance`
  validates `--operation` / `--acquired-at` up front; and `cluster
  update`'s `--target` / `--target-sha` combination checks (against
  `--restart-only` / `--local` / `--force` / `--dry-run` / `--mode force`)
  move from the command body to the parse layer. All of these are usage
  errors (exit 2) before any command code runs; the command-level guards
  stay as the programmatic-caller defense. Retained defaults (monitoring
  verbs, display bounds, safe-mode switches) now carry their reasons behind
  a `task #4092 cli-default inventory` marker; the discipline and the
  inventory entry point are documented in
  `docs/conventions/engineering/cli-argument-discipline.md`.
- CLI parameter discipline, batch B3 (task #4092): `mcp add` requires exactly
  one of `--json` / `--command` and validates both at the parse layer (bad or
  non-object JSON, a `--env` pair without `=`, and `--arg`/`--env` without
  `--command` are usage errors), `mcp install --env` validates `KEY=VALUE`,
  and `meiva packages policy` requires at least one field with `--check-every`
  validated at parse time.
- `meiva impersonate request` / `renew` state their time parameters explicitly
  (task #4102): `request --ttl` and `--batch-window` and `renew --ttl` are
  required — missing values are usage errors before any command runs — the
  request's relay shape is validated at the CLI boundary, and the takeover
  launch message spells out the one-hour recovery deadline and immediate
  delivery it previously inherited from defaults.
- The Claude Code Monitor relay guidance now reflects per-watch deadlines
  (Claude Code 2.1.271+): arm with `timeout_ms: 1800000` and re-arm on the
  expiry notice — at the deadline the watch and the relay process it runs are
  killed, and a missed re-arm stops the lease (task #4037).
- A takeover no longer outlives a dead core component, and a dead relay is no
  longer silently respawned (task #3998, user ruling 2026-09-18): the accepting
  runtime re-checks the recorded executor process chain and the bound relay's
  heartbeat on every held pass — the dispatcher's pull scan now keeps held rows
  in its periodic cadence — and closes the lease when every anchor is
  dead/reused or the heartbeat is stale past 45 seconds: status `expired`, the
  cause recorded as `aborted: <detail>`, a relay process still held
  terminated, and an end-of-session note naming the cause. The single
  restart-shaped exception — a codex relay minted by an earlier incarnation,
  last beat before this process start, inside the fresh-start window
  (`MEIVA_IMPERSONATION_REPROVISION_WINDOW_SECONDS`, default 120 s, 0 disables)
  — is re-provisioned instead of stopped.
- The impersonation relay delivers routine inbox arrivals immediately unless a
  lease's merge window says otherwise: the per-lease window
  (`relay_batch_window_seconds`) is stated explicitly at request time
  (`--batch-window`, 0..300 seconds since task #4102); user chats, cancels and
  renewal reminders never wait in any case (task #3997).
- A macOS `meiva start` whose own chain runs outside the GUI login session no
  longer brings services up in place (they would inherit the wrong launchd
  domain — the state that wedges the shared browser): an operator-shaped start
  hands the bring-up to the cluster's GUI-domain autostart job (`kickstart -p`,
  which never kills a running instance), waits for the job to reach its launch
  step, and answers with the same readiness exit contract as a normal start.
  Internal restart legs (update / `meiva restart` / the stop compensator) keep
  their in-place behavior — their credential-handover marker cannot cross
  domains and their flags are not equivalent to the canonical job — and
  `MEIVA_START_GUI_HANDOVER=0` restores the previous warn-only behavior
  (task #3348).
- PR merge automation now uses Trunk Merge Queue: queued PRs are tested
  together in batches (target 4, max wait 5 min) via draft-PR runs on `trunk-merge/*`
  branches; the merge gate is the full CI job set (12 required statuses: the three
  suite aggregators, the non-matrix jobs, and the `qa-approved-gate` check); the
  `qa-approved` label remains the hard pre-merge gate (branch protection + queue
  admission).

### Fixed
- Inbound state semantics converge sooner and never double-show during a
  takeover (task #3999, from the #3683 duplicate display): every finished
  hosted turn now reconciles its own claimed `chat` rows at settlement — the
  throttled checkpoint tail is flushed first (`MEIVA_HOST_TURN_RECONCILE_ENABLED`)
  — so a handled message reaches `done` without waiting for the next boot or
  abort, and the pending strip (`GET /api/agents/{id}/pending`) hides a chat
  the active session absorbed: while an unexpired active session exists, a
  pending chat that the session trail transcribed or the relay read appears
  in the timeline only, and returns to the strip if the session ends
  unacknowledged. The row itself stays `pending`; the strip is a read-surface
  view.
- A `plugins_config.json` entry whose plugin directory is gone is now reported
  through the canonical plugin-load reporter (loguru ERROR + a
  `plugin_load_failed` event, once per process) instead of a plain warning —
  the dangling `codex_usage` / `deepseek_balance` entries on a 2026-09-11
  host stayed invisible to every alert surface while their plugins were
  silently disabled (task #3918).
- Hosted-force recovery no longer defers forever on an unreadable exec request
  envelope. An envelope whose own bytes cannot be read — the zero-byte or
  partial remnant a killed parent leaves — carries no attribution, so it is
  now bounded by the protocol: past twice the exec node timeout, with no live
  process reference and no live host process, it is quarantined (bytes
  preserved with a receipt) and counted, raising the
  `exec_request_bounded_quarantine` anomaly event; the retention switch is
  `MEIVA_EXEC_REQUEST_BOUNDED_QUARANTINE_ENABLED`. Exec envelopes are also
  written atomically (temp file + fsync + rename), so a killed writer cannot
  produce that remnant in the first place, and a boot recovery that stays
  deferred across three consecutive boots escalates to the
  `hosted_boot_recovery_stalled` anomaly event instead of one silent per-boot
  warning (task #3619).
- The hosted dispatcher re-asserts a pending cancellation after crossing its
  database scan: a cancel that lands inside psycopg_pool's async connection
  check is absorbed there (the pool returns the connection and retries without
  re-raising that `CancelledError` — upstream psycopg#1345, still present in
  3.3.1), which left the cancelled task looping forever with its canceller hung
  in `await task` (task #3513).
- Every automatic meiva-browser rebuild now routes through the GUI domain when
  the healthcheck's own chain runs outside the macOS GUI login session — the
  session-gone sweep and the live-session dead-CDP respawn stop the stuck
  session and kickstart the cluster's GUI-domain autostart job instead of
  re-creating a context-less session in place (the residual path that kept the
  wrong-domain loop alive after the task #3149 heal). The rebuild line is a
  WARNING naming the trigger and a cumulative attempt count, service respawns
  record their chain's launchd domain for attribution, and `meiva start` warns
  loudly when an agent-runner host is started from a chain outside the GUI
  login session (task #3346).
- A browser daemon chain that lost the GUI login session no longer waits
  forever for a Keychain it can never read: the macOS readiness probe names
  the missing context (`launchctl managername` ≠ `Aqua` — the state a respawn
  from an agent or SSH chain lands in, where securityd denies every
  login-Keychain query), and the healthcheck reacts by stopping the stuck
  session and kickstarting the cluster's GUI-domain autostart job instead of
  waiting in place — bounded to two attempts per episode, with its own
  in-context rebuild deferred while that relaunch lands. The CDP probe also
  stops treating every unusable `/json/version` answer as "respawn him": an
  answered port that is not this cluster's Chrome is `PORT_TAKEN` (no
  respawn churn), while our own wedged endpoint stays `DOWN` and heals
  through the existing sweep + rebuild (tasks #3149, #2692).
- Page ids are no longer trusted across an upstream reconnect: the browser-mcp
  daemon reconnects `chrome-devtools-mcp` in place, and the new process
  renumbers pages from 1 — a surviving page id could previously re-pin an
  agent's next call onto a different tab, or let the page-TTL sweep close one.
  Page-keyed state (per-agent affinity, TTL slots, the legacy per-connection
  page) now carries the upstream connection generation that minted it; after a
  reconnect stale slots read as no-page/expired and agents rebuild through the
  existing cold-start paths (task #3048).
- Listener discovery no longer reads a restricted inspection context as
  absence: the lsof fallback resolves through standard absolute locations when
  a context's PATH omits it (macOS keeps lsof in /usr/sbin, which cron's
  /usr/bin:/bin PATH never reaches — company-air `meiva status` reported a
  healthy collector as "nothing listening on port 4318" for exactly this
  reason), and a psutil scan that cannot attribute the port's LISTEN socket
  (a `pid is None` row) falls back to lsof instead of reporting empty; when
  psutil sights a listener that lsof cannot confirm, discovery raises instead
  of certifying absence (task #2665).
- New shell session transcripts start with a unique host-written identity line,
  preventing filelog fingerprint collisions from shared login or CLI banners.
- Single-machine dual-unit telemetry port deviation: a unit whose local
  collector deviates (`MEIVA_TELEMETRY_OTLP_PORT` 4319, e.g. the WSL unit beside
  a Windows collector on 4318/8888) no longer derives its remote-station
  export target from its own port — station relays and probes use the station's
  advertised ingress (or `MEIVA_OBSERVABILITY_OTLP_PORT`), so telemetry stops
  black-holing to a port the station never listens on. Local collector liveness
  probes bind the unit's own receiver port independently of the producer export
  override, and a failed listener inspection reports `unavailable` instead of
  impersonating "nothing listening" (no unsafe respawn; task #2587).

- Maintenance drain receipts are graded by failure class: a turn raising a
  database-outage exception (`psycopg.OperationalError`, `PoolTimeout`)
  records a crash-equivalent `undelivered` receipt instead of a
  blocking failure — the hold stays resumable, and the host re-drives the
  held-control path (explicit re-flush before the restart claim) on the next
  wake, so a drain can never certify through an un-flushed tail. Blocking
  failures get a sanctioned exit: `meiva maintenance repair --operation <op>
  --acquired-at <ts> [--operator ...]` moves them into an audited `repaired`
  record (operator identity + timestamp, both CAS sides visible in `meiva
  maintenance status`) and releases the hold (2026-09-08 network degradation:
  38 PoolTimeout receipts permanently wedged the machine with no repair path).
- Plugin loading is fail-soft: a plugin whose `plugin.py` raises at import (or
  a config entry whose plugin directory is gone) is skipped with a loud
  `plugin_load_failed` event + log instead of crashing `import meiva` cluster-wide
  (2026-08-28 meiva_ledger incident). External plugins' `from . import x` relative
  imports now resolve via a framework-registered `plugins.<name>` namespace
  regardless of sys.path/cwd, and `meiva plugins install`/`upgrade` land the
  plugin tree atomically (staging + rename, previous version restored on
  failure) so a half-installed plugin can no longer exist.
- Plugin-load containment now covers every load path, not just the graph build
  (issue #2161, user ruling 2026-09-11). The host-boot loader imports through
  the same primitives as the graph-build loader — same dotted module name, same
  `sys.modules` identity, same fail-soft skip + loud `plugin_load_failed`
  report — so a broken `plugin.py` can no longer take the agent host down on
  every restart (2026-09-10 agent-host incident), and a plugin disabled in
  `plugins_config.json` is imported by no production path (the boot loader
  previously ignored the enable set). The same per-plugin containment now
  covers `provider.py`, `services.py`, `setup.py`, a built-in `metrics.py`, and
  the launched child's `import meiva` self-load. Deliberately still fail-closed
  (no guess at operator intent): duplicate plugin names, a malformed
  `plugins_config.json`, plugin-config schema drift, provider
  registration-contract violations, and the provider loader's post-load
  cross-model revalidation. The release probe substitutes the canonical
  fail-soft reporter, so a candidate image with unloadable plugin code is
  rejected instead of degraded.
- Release preparation now exercises provider registration alongside the
  `plugin.py` and `services.py` faces: the probe keeps the image's own
  provider-bearing built-ins enabled next to the retained set (the provider
  loader rejects an empty binding set), so a retained `provider.py` that
  fails to load rejects the candidate instead of shipping and silently
  degrading the model registry after rollout (task #3008).
- Session-revoke suffix fallback could revoke the request's own session when
  called with its masked suffix (the logout-only guard was bypassed) and
  accepted any id shape of 8+ characters as a suffix. The fallback now only
  triggers for the exact 8-character masked form and excludes the current
  session from matches.
- The `qa-approved-gate` label check now reads the PR's labels live from the
  API at evaluation time instead of the event payload: a run queued before the
  label was applied can no longer execute late and flip the check's newest
  result back to failure after the labeled run passed, and a stale payload
  that still lists a removed label can no longer pass. An unlabeled PR is
  still red; the Trunk exemption is unchanged (task #3334).

### Added
- Browser-mcp daemon now keeps a valid gateway session cookie in the shared
  managed Chrome: logs in through the gateway, injects the returned opaque
  server-side session over CDP (`Network.setCookie`), and refreshes every 6h.
  The managed browser can open auth-gated gateway URLs (agent-served pages
  behind the gateway reverse proxy) on any machine, fresh profile or not.
- The browser-session list masks non-current session ids to their final 8
  characters (the revoke endpoint accepts the suffix) and labels
  managed-browser sessions; the managed browser re-checks its gateway session
  after gateway-URL navigations and refreshes early when it no longer
  authenticates.
- Delivery observability: `inbound_messages.claimed_at` (set on claim; pickup
  latency = claimed_at - created_at), degraded idle-wake logged at WARNING when
  the pub/sub fast path is lost, and a gateway delivery watchdog that alerts on
  chat inbounds still pending past 30s (once per row while stuck).
- Delivery watchdog now also **dispatches lost wakes**: on a 0.5s tick it
  re-publishes the Redis wake (with the wake-key breadcrumb) for every pending
  inbound of an idling owner older than 1s — a lost pub/sub publish recovers in
  ~1.5s instead of the claim loop's 30s recheck. Constant ~2 qps load,
  independent of fleet size.
- Open-source readiness: Apache-2.0 license + third-party notices,
  community-health files (CONTRIBUTING / CODE_OF_CONDUCT / SECURITY / templates),
  and a forkable CI lane for GitHub-hosted runners.
- `install.sh --mirror cn`: route PyPI / npm / Homebrew through China mirrors.

### Changed
- Cross-machine file transfer no longer hard-requires a shared Google Drive
  folder: `MEIVA_REQUIRE_GOOGLE_DRIVE` is replaced by the configurable
  `MEIVA_CROSS_MACHINE_TRANSFER_BACKEND` (`drive` | `none`, default `drive`), and
  the agent-runner converge step probes the backend and warns instead of
  blocking start when Drive is unavailable.
- The local trace mirror now stays disk-bounded end to end: the collector's
  file exporter rotates `spans.jsonl` at 64 MiB (was 256) with 24 backups,
  old segments are gzipped to `.jsonl.gz` by an agent-start pass (5-10x
  smaller), and the agent-side retention/dir-cap prunes now recognize the
  timberjack-suffixed rotated names (`spans-<ts>-size.jsonl`), the manual
  `spans.cut-*` shape, and `.gz` variants — previously those files were
  invisible to retention and could even outlive the active file in a cap
  prune. `meiva trace ship` and the inspect-a-trace mirror fetcher read gzipped
  segments transparently, continuing from the same per-file watermark.
- Pure agent-runners now relay OTLP traces/logs/metrics through a
  bearer-authenticated gateway collector receiver instead of writing to
  nonexistent loopback LGTM backends. Tempo/Loki/Prometheus stay gateway-local;
  collector queue/drop/silence alerts cover delivery failures.
- Session stdout is ingested into Loki through disjoint collector filelog
  receivers: agent shells separately from gateway/daemon/schedule output, with
  banner-only agent main logs excluded. In coordination with #3279, 10-second
  polling, EOF metadata archival, bounded discovery, and daily-throttled local
  7-day `*.out.log` retention prevent content-fingerprint re-watch storms.
- Genericized author/prod identifiers (container registry, host addresses, repo
  slugs) out of code, CI, and tests.

### Removed
- The one-time fleet cutover tooling: `scripts/cutover_*` (home adoption,
  inventory, legacy OS jobs, data-plane authority conversion, database-records
  survey and repair), `cli/cutover_hold.py` with the start / `meiva maintenance`
  / `meiva cluster recover` refusals of the cutover hold,
  `shared/predecessor_closure.py`, the `CutoverAuthority` ledger capability
  and their runbooks. A home born before the always-authenticated data plane
  is refused and no longer names a conversion.
- `meiva logs` CLI (list live sessions / tail one session's log) — replaced by
  the Loki query path above; see `deploy/lgtm/README.md`.

### Security
- Secrets no longer ride process command lines. Session env is handed to the
  child out-of-band (a 0600 env file, never an argv splice), the per-agent
  config overlay travels in the child's environment, and the cluster's
  redis takes `requirepass` from a config file / `$REDISCLI_AUTH` — so `ps` no
  longer shows the cluster secret, the data-plane URLs, or provider API keys to
  other local users.
