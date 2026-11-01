---
type: doc
title: meiva_usage — Scheduled Multi-Account Usage and Balance Collectors
description: '`meiva_usage` polls model-provider usage and balance endpoints across multiple accounts and machines, surfacing live status cards and recording metrics.'
tags:
- extensions
- plugin
- usage
---

# meiva_usage — Scheduled Multi-Account Usage and Balance Collectors

## What is it

`meiva_usage` is a built-in plugin that periodically queries account balances, credit limits,
and usage metrics from LLM providers (`claude`, `codex`, `antigravity`, `deepseek`, `openai`, `anthropic`) across
multiple accounts and target machines, and publishes current readings into `plugin_stats`.

Each collector is declared in the cluster-pinned configuration (`configs/meiva_usage/config.json`)
via `CollectorConfig`. Through declarative contribution faces, the plugin provisions both:
1. **Dynamic status cards** (`ui.statsFace` -> `ui_stats.py`): displayed in the Meiva UI dashboard.
2. **Declarative background schedules** (`schedules.face` -> `schedules.py`): reconciled into the
   cluster database and launched on target machines by the `ScheduleManager`.

## Architecture & Components

### 1. Multi-account collector configuration (`default_config.py`)

`UsageConfig` carries a tuple of `CollectorConfig` items scoped as `cluster-pinned`:
- `provider`: One of `claude`, `codex`, `antigravity`, `deepseek`, `openai`, `anthropic`.
- `machine`: Target cluster machine where the probe runs (e.g. `gateway`, `company-air`).
- `interval_minutes`: Polling cadence in minutes.
- `key_env`: Environment variable name supplying the provider API key (e.g. `DEEPSEEK_API_KEY`, `OPENAI_ADMIN_KEY`, `ANTHROPIC_ADMIN_KEY`).
- `credentials_path`: Explicit credentials JSON path (for Claude session tokens or OAuth tokens).
- `keychain_account`: macOS Keychain account identifier (for reading Claude CLI OAuth credentials).
- `account_email`: Account identifier (for Antigravity or Codex RPC multi-account selectors).
- `oauth_client_id_env` / `oauth_client_secret_env`: Process environment variable names for OAuth flows.
- `known_balance_usd`: Optional prepaid dollar baseline for `openai` and `anthropic` cost collectors.
- `balance_as_of`: Optional UTC ISO date (`YYYY-MM-DD`) anchoring the prepaid baseline; spend on or after this date is subtracted from `known_balance_usd`.

### 2. Declarative faces

- **UI stats face (`ui_stats.py`)**:
  Contributes `tuple[StatCardSpec, ...]` for all collectors with `enabled=True`. Evaluated
  dynamically against the current `UsageConfig`, generating status cards rendered by the gateway
  in `/api/ui/contributions`.

- **Schedules face (`schedules.py`)**:
  Contributes `tuple[ScheduleSpec, ...]` for active collectors (`enabled=True`). Each schedule
  embeds a canonical `# collector_spec: {...}` JSON comment in its script. When an operator
  modifies collector intervals or parameters, the script changes deterministically, prompting
  `reconcile_plugin_schedules` to bump `desired_revision` and converge the running session.

### 3. Schedule runner & probe execution (`runner.py`)

The schedule script executes `run_from_schedule(MEIVA_SCHEDULE_INPUTS, collector_id=...)`:
- **Config resolution**: Resolves configuration through `read_plugin_config`, supporting local
  gateway checkouts as well as split agent-runners that receive `MEIVA_PLUGIN_CLUSTER_CONFIG`
  via `GET /api/bootstrap`.
- **Machine attribution**: Passes the current machine identity to `refresh_collector_once`.
- **Error hysteresis (`should_write_failure`)**: Transient network or probe errors within the
  freshness window do not flap cards to error immediately, preserving previous good readings.
- **Single writer (`stats.upsert`)**: Writes readings through `base.packages.plugins.surface.stats.upsert`,
  stamping `updated_by` with the machine name.

### 4. Process-scoped secret delivery

Collectors declare sensitive environment variable names via `secret_env_vars()` (e.g. `key_env`).
These names are stored in `schedules.secret_env_vars`. When the `ScheduleManager` launches a
resident session:
- The daemon resolves secret values from the local `.env` and process environment.
- On split remote runners, the daemon dials the gateway's `GET /api/schedules/{id}/launch-env`
  using the machine's bearer token.
- The resolved secrets are merged directly into the child session's execution environment (`env`),
  keeping process command-line arguments and database schedule rows clean of credentials.

### 5. Organization admin cost probes (`openai`, `anthropic`)

For providers without a direct balance endpoint, `meiva_usage` polls their official Organization Admin Cost APIs:
- **OpenAI**: `GET /v1/organization/costs` paginated with daily buckets in direct USD dollars.
- **Anthropic**: `GET /v1/organizations/cost_report` paginated with daily buckets where amounts are in lowest currency units (cents, divided by 100 for USD).

Card formatting operates in two modes:
1. **30-day trailing spend (default)**: When `known_balance_usd` is not configured, the card displays 30-day cumulative spend (e.g. `$42.50 (30d)`) and today's spend in the detail line.
2. **Prepaid baseline subtraction**: When `known_balance_usd` and `balance_as_of` (`YYYY-MM-DD`) are both configured, the probe computes remaining prepaid credit by subtracting spend since `balance_as_of` from `known_balance_usd` (e.g. `$85.20 left`), entering `"warn"` status when remaining balance drops to $\le 10\%$ of baseline or $\le \$5.00$.
