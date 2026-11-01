# Plugin-declared machine-placed schedules and the `meiva_usage` collector plugin

> **Status: active implementation plan.** Covers three slices:
> (S1) machine-placed schedules with one `schedule-manager` per machine,
> (S2) declarative plugin schedule and config-derived status-card extension points
> with process-scoped secret delivery, and
> (S3) consolidating the four external usage/balance plugins into the builtin
> `meiva_usage` plugin.

## Context and problem

Four external plugins (`claude_usage`, `codex_usage`, `antigravity_usage`,
`deepseek_balance`) were hand-placed into per-machine `$MEIVA_HOME/plugins/`
directories without install-registry entries, version bounds, or tests:

1. **Wrong execution model.** Each plugin registered `after_init` and `after_exec`
   agent graph hooks, probing external HTTP APIs or local CLIs inside agent host
   processes. When no agent ran turns, status cards silently aged; when multiple
   machines installed the same plugin, machines raced on shared `plugin_stats`
   rows and needed staleness heuristics to avoid clobbering each other.
2. **Broken kernel coupling.** Two of the four plugins imported internal
   `Database.from_settings()` without a `ProcessDbGate`, failing on every hook
   invocation after the entry-owned gate refactor and leaving cards stale without
   being able to write `status='error'`.
3. **Entangled placement and credentials.** Account credentials live in different
   places: cluster API keys (`DEEPSEEK_API_KEY`, secondary keys) live in the
   gateway `.env`, while CLI login sessions (`~/.claude`, `~/.codex*`,
   `~/.gemini`) live on specific machines and must never be copied into shared
   database rows. Meanwhile, Meiva's `schedules` table had no `machine` column
   and `schedule-manager` ran only on the gateway, while `ServiceSpec` daemons
   require a closed fixed-port slot per daemon.
4. **Static card declarations.** `contributions.ui.stats` in `meiva-plugin.json`
   is a static list, so configuring multiple accounts (for example two DeepSeek
   keys or two Codex homes) could not declare N cards without hardcoding personal
   account identifiers into the manifest.

## Design principles

Every surface introduced here follows six rules:

1. **Mechanism in core, policy in plugins (Microkernel).** Core owns machine-placed
   schedules, reconciliation, process-boundary secret delivery, and status-card
   rendering; `meiva_usage` owns provider probes, window parsing, and card text.
   Adding a fifth usage provider touches zero core files.
2. **Declarations are data; behavior is code (Extension points).** Plugins expose
   pure functions returning frozen spec tuples (`ScheduleSpec`, `StatCardSpec`)
   from their validated configuration. Tooling (`meiva plugins inspect`) and the
   gateway inspect declarations without running probes.
3. **Framework-owned cleanup via attribution (`owner_plugin`).** `schedules` rows
   created for a plugin record `owner_plugin`. Disabling a plugin or removing a
   collector entry reconciles its owned rows out and reaps live sessions without
   imperative teardown hooks.
4. **Capabilities at process boundaries, never ambient secrets.** Plugin config
   JSON remains non-secret (`cluster-pinned`). A `ScheduleSpec` names required
   environment variable identifiers (`secret_env_vars`), and `ScheduleManager`
   delivers only those variables into that schedule's session request body.
5. **Central desired state, node-autonomous execution.** The gateway is the sole
   reconciler writing builtin and plugin-declared rows into `schedules`. Every
   machine runs its own `schedule-manager` (`meiva_runner` role), executing only
   rows where `machine` equals its own machine name. Unrunnable placements
   (unknown, paused, stopped, or offline machine) surface explicitly in API and
   CLI status rather than failing silently.
6. **Public contracts only.** Collector scripts obtain `Database`, `Clock`, and
   machine identity from `open_schedule_entry` and write cards via
   `base.packages.plugins.surface.stats.upsert`.

---

## Slice 1 — Machine-placed schedules and per-machine `schedule-manager`

### Database schema and grants

One migration updates `schedules`, `schedule_sync_requests`, and `meiva_runner`
privileges (mirrored in `db/schema.sql` and `base/cluster/authority/groups.py`):

- `schedules`:
  - `machine TEXT NOT NULL CHECK (btrim(machine) <> '')` — backfilled on existing
    rows from the gateway unit in `machine_units` (`WHERE serve_gateway`, falling
    back to `machines` with `'gateway' = ANY(role)`). No column default: callers
    must name a target machine explicitly.
  - `owner_plugin TEXT CHECK (owner_plugin IS NULL OR btrim(owner_plugin) <> '')`
    — `NULL` for operator and builtin core schedules; plugin directory name for
    plugin-contributed schedules. Indexed where non-null.
  - `secret_env_vars TEXT[] NOT NULL DEFAULT '{}'` — environment variable names
    injected into this schedule's session at launch.
  - Index `ON schedules (machine, enabled)`.
- `schedule_sync_requests`:
  - Add `machine TEXT NOT NULL CHECK (btrim(machine) <> '')` and change the
    primary key to `PRIMARY KEY (schedule_id, machine)`.
  - Moving a schedule from machine A to machine B enqueues both `(id, A)` and
    `(id, B)`: A's manager reaps its local session and deletes `(id, A)`, while
    B's manager launches the session locally and deletes `(id, B)`. Deleting a
    schedule reads its `machine` before deleting the row and enqueues `(id, A)`.
- `meiva_runner` grants:
  - `GRANT SELECT, UPDATE ON schedules TO meiva_runner`
  - `GRANT SELECT, INSERT, UPDATE ON schedule_runs TO meiva_runner` and
    `GRANT USAGE, SELECT ON SEQUENCE schedule_runs_id_seq TO meiva_runner`
  - `GRANT SELECT, DELETE ON schedule_sync_requests TO meiva_runner`
  - `GRANT SELECT, INSERT ON schedule_fire_log TO meiva_runner` and
    `GRANT USAGE, SELECT ON SEQUENCE schedule_fire_log_id_seq TO meiva_runner`

### Supervision, configuration, and node execution

- **Roster**: `schedule-manager` moves to `both_services` in
  `services/supervision/roster/composition.py` with `capabilities=BOTH`,
  `requires_db=True`, `db_access="runner"`. Running `schedule-manager` under the
  `runner` login on both gateway and agent-runner units enforces least privilege
  and eliminates single-box vs. split-runner permission skew. Fixed port slot
  `8122` already exists in `FIXED_PORTS`; runner homes require no port-record
  migration.
- **Config capability**: `schedule_manager_health_port`,
  `schedule_auto_restart_max_per_window`,
  `schedule_auto_restart_window_seconds`,
  `schedule_recovery_gate_alert_seconds`, and `schedule_stall_timeout_seconds`
  move from `capability="gateway"` to `capability="common"`, regenerating
  `base/host/env/config_lite_table.json`.
- **Partitioned loops**: `ScheduleManager` binds `machine_name` at construction:
  - `reconcile` loads `WHERE enabled AND machine = %s` and scopes stall-timer
    cleanup to `AND machine = %s`.
  - `revisions.desired` verifies `machine = %s` so a manager never mutates
    another machine's row; local `meiva-schedule-<id>` sessions whose row was
    deleted or moved to another machine are reaped.
  - `consume_requests` selects and deletes `WHERE machine = %s`.
  - Co-located split units sharing one `machine_name` elect one active unit home
    deterministically via `machine_units` (`stopped_at IS NULL`, preferring
    `serve_gateway DESC, home ASC`) so two `pty-sessions` sockets on one box
    never contend over `launch_generation`.
- **Runner process boot**: `services/wake/schedule_manager/runner.py:main` boots
  its `ConfigBoot` at process entry (keeping in-process `run()` non-mutating for
  tests), so split-runner schedule sessions consume `db-authority/unit.json` and
  `/api/bootstrap` automatically.
- **Gateway seeding ownership**: `provision_builtin_schedules` runs in the
  gateway process lifespan (and `meiva schedules provision`), never inside the
  `meiva_runner`-privileged `schedule-manager`, seeding builtin rows with
  `machine = <gateway_machine_name>`.

### API, CLI, explicit placement status, and remote logs

- `/api/schedules` (`ScheduleSummary`, `ScheduleView`, `ScheduleCreate`,
  `ScheduleUpdate`) and `meiva schedules` (`ls`, `get`, `create --machine`,
  `update --machine`) carry `machine` and `owner_plugin`. `ScheduleCreate.machine`
  defaults to the gateway's own machine name when omitted.
- **Explicit unrunnable state**: `GET /api/schedules` joins `machines` and
  `machine_probe` to return `placement_status` (`"ok"`, `"unregistered"`,
  `"paused"`, `"stopped"`, `"offline"`) and `placement_reason: str | None`,
  surfaced in `meiva schedules ls` and `/control/schedules`.
- **Cross-machine logs**: `GET /api/schedules/{id}/logs` reads the local shell
  backend when `schedule.machine == local_machine`, and otherwise dispatches a
  `schedule_capture` RPC to the target machine's `agent_ops` daemon (mirroring
  `shell_capture`).

---

## Slice 2 — Plugin schedule & dynamic status-card extension points

### Declarative faces and manifest keys

- `meiva-plugin.json`:
  - `"schedules": { "face": "schedules.py" }` declares that the plugin exports a
    pure schedule declaration face.
  - `"ui": { "stats": [...], "statsFace": "ui_stats.py" }` keeps static card
    declarations working while allowing config-parameterized plugins to declare
    cards via `ui_stats.py`.
- `base/packages/plugins/surface/schedules.py` defines frozen `ScheduleSpec`:
  - `name: str` (globally unique, prefixed `<plugin>:` or `<plugin>-`)
  - `machine: str`
  - `description: str`
  - `script: str`
  - `command: str`
  - `secret_env_vars: tuple[str, ...] = ()` (validated `[A-Z][A-Z0-9_]*`,
    forbidding `MEIVA_*` authority/identity overrides)
  - `default_enabled: bool = True`
- `schedules.py` exports `contribute(config: C) -> tuple[ScheduleSpec, ...]`.
- `ui_stats.py` exports `contribute(config: C) -> tuple[StatCardSpec, ...]`
  (`StatCardSpec(id: str, label: str)`). `GET /api/ui/contributions` merges
  static `ui.stats` with `ui_stats.py` declarations for enabled builtin plugins
  against their current config image (fail-soft via `load_report`, matching
  `inspector.py` and `metrics.py`).
- `meiva plugins inspect` includes the `schedules` surface in `SURFACES` and
  renders the resolved `ScheduleSpec` identifiers (`<name>@<machine>`).

### Level-triggered gateway reconciliation and secret delivery

- **Controller**: A lightweight reconciler in the gateway checks the input digest
  over `(plugins_config.json, configs/<plugin>/config.json)` for plugins
  declaring `schedules` on startup, periodically (every 5 s, stat-only when
  unchanged), on `PUT /api/config`, and in `meiva schedules provision`:
  - Missing declared row -> `INSERT` with `owner_plugin = plugin`, initial
    `schedule_versions` entry, and `(id, spec.machine)` sync request.
  - Existing row with `owner_plugin == plugin` -> preserves `enabled`; if
    `(machine, description, script, command, secret_env_vars)` changed, updates
    the row, increments `desired_revision`, records `schedule_versions`, and
    enqueues sync requests for both old and new `machine`.
  - Existing row with `owner_plugin != plugin` -> reports a plugin load error and
    refuses to overwrite another owner's schedule.
  - Rows in `schedules` with `owner_plugin IS NOT NULL` that are no longer
    declared by any enabled plugin -> `DELETE` + enqueue `(id, old_machine)` sync
    request so the owning machine terminates the session immediately.
- **Secret delivery**:
  - Gateway `/api/bootstrap` augments its secret allowlist with the union of
    `secret_env_vars` on enabled `schedules` rows placed on split runners, reading
    values from the gateway `.env` alongside `provider_key_envs`.
  - `ScheduleManager._launch` resolves `row.secret_env_vars` from the unit's
    delivered secret source (`.env` on gateway, bootstrap env on split runners)
    and injects only those keys into that session's `new_session(..., env=env)`
    payload. Schedules declaring `secret_env_vars = ()` receive zero extra keys.

---

## Slice 3 — Builtin `meiva_usage` plugin

Consolidates the four external collectors into `meiva_builtins/plugins/meiva_usage/`:

- **Config (`default_config.py`)**:
  - `UsageConfig(collectors: tuple[CollectorConfig, ...] = ())` with
    `scope="cluster-pinned"`. Repo default is empty — zero personal emails,
    labels, machine names, or secrets in source.
  - `CollectorConfig`:
    - `id: str` (status card id, e.g. `"deepseek-main"`, `"claude-air"`)
    - `label: str` (status card label)
    - `provider: Literal["claude", "codex", "antigravity", "deepseek"]`
    - `machine: str` (machine where the schedule runs)
    - `interval_minutes: int = Field(default=10, ge=1, le=1440)`
    - `enabled: bool = True`
    - Provider-specific non-secret source selectors:
      - `key_env: str | None = None` (e.g. `"DEEPSEEK_API_KEY"`,
        `"DEEPSEEK_API_KEY_2"`)
      - `credentials_path: str | None = None` (optional file path override for
        Claude / Antigravity / Codex home)
      - `keychain_account: str | None = None` (optional macOS Keychain account
        for multiple Claude Code logins)
      - `account_email: str | None = None` (Codex account matcher across
        `~/.codex*` homes)
      - `oauth_client_id_env: str | None = None` and
        `oauth_client_secret_env: str | None = None` (optional env-var names for
        Antigravity token refresh, keeping OAuth client secrets out of source)
- **Faces**:
  - `plugin.py`: empty SDK surface (`PluginContributions()`), so agent and exec
    processes carry zero overhead.
  - `schedules.py`: maps each `CollectorConfig` to one `ScheduleSpec` running
    `.venv/bin/python -m meiva_builtins.plugins.meiva_usage.runner <id>` on
    `collector.machine`, declaring `secret_env_vars` from `key_env` /
    `oauth_client_*_env`.
  - `ui_stats.py`: maps each `CollectorConfig` to `StatCardSpec(id=c.id, label=c.label)`.
- **Execution (`runner.py` + pure core + provider probes)**:
  - Uses `open_schedule_entry` for gated `Database`, `Clock`, and machine name,
    running an interval loop that writes `plugin_stats` via `stats.upsert`.
  - Pure strict modules (`"strict"` in `meizel.toml`) own window parsing,
    percentage math, English card formatting, and error-clobber decisions; thin
    effect shells receive `user_home`, `environ`, `now`, HTTP/subprocess runners,
    and `Database` explicitly from `runner.py` (`[roots]`).
  - Zero raw CJK in source, comments, docstrings, or tests.
