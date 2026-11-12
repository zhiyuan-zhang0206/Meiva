# meiva.shell — Facade and Implementation

## Responsibility

The agent-visible entries of [meiva.shell](shell.md) are thin facades.
Each reads what it needs from the process once (the agent identity, the workspace, the
configured capture window, the `meiva` CLI beside the interpreter, the machine facts a
new session starts from) and hands those values to an implementation. The public
signatures are the SDK's contract and do not change for a test's sake; the
implementations take every dependency as an argument, so a test calls them with values
and a real resource.

## Modules

- `meiva/shell/sessions.py:ShellSessions` — one agent's sessions on the pty-sessions
  backend and the cluster database; its constructor and `create` take everything they use.
  `shell_sessions()` is the facade root (it refuses a process without an agent identity),
  `open_sessions` builds one for a given agent id, `process_environ` reads the environment
  the bound context carries, and `session_host` builds the machine facts a session forwards
  from that value and this checkout.
- `meiva/shell/session_plan.py` (strict) — what a session may be called and live for
  (`validate_name`, `validate_ttl`) and how session ids map onto the names the backend
  lists (`match_session`, `session_table`).
- `meiva/shell/session_env.py` (strict) — the environment a new session starts with:
  `SessionHost` is the checkout and the forwarded environment a root read once;
  `session_environment` drops `VIRTUAL_ENV` for a session outside the checkout, names the
  owner and applies a watcher's overrides.
- `meiva/shell/ttl_store.py` — the SQL behind a deadline: the per-agent counter, the
  mandatory `agent_shell_ttls` row, the guarded renewal and its audit row. It imports
  psycopg, so `ShellSessions` loads it when a session is created or renewed.
- `meiva/shell/background.py:start` — runs a `BackgroundRun` (the validated command, name,
  directory, keep, ttl and notify policy) with a `BackgroundSite` (the agent, the CLI that
  delivers the notice, the log directory, the machine facts). A run that fails after its
  session exists removes the launch file and kills the session, whatever step failed.
- `meiva/shell/background_plan.py` (strict) — the single line a background session runs
  (`notified_line`), the launch file that carries it and the notify policy (`NotifyPolicy`:
  `notify_policy` turns the caller's text into a member or refuses it with a `ValueError`,
  and `notified_line` takes only members).
- `meiva/shell/log_ring.py` (strict) — where the logs live and which ones the per-workspace
  ring evicts; live-session logs and logs awaiting completion delivery survive the cap.

## Tests

Tests of the implementations run against a real pty-sessions service and the throwaway
database (`meiva/shell/tests/live_shell.py`: `live_shell`, and `live_host` whose HOME is the
test home, so a session never reads the developer's profile or finds the developer's
programs). The completion CLI is a small script that records its argv. The SDK entries are
tested in `meiva/tests/namespaces/shell` with the bound agent and the same service.

## Completion recovery boundary

A completed command and an accepted completion notice are separate outcomes.
The shell preserves the command status even when the gateway is unavailable.
It publishes a `.undelivered` intent beside its output before sending; failed
or uncertain sends leave that intent and log available for operator recovery.
The original send and replay use the same gateway client-message identity,
so acceptance followed by a lost response can be retried without another message.
This applies to shell-level completions, including watchers; it does not replay
a watcher's own application messages or rerun its program.

The operator wires `scripts/host_ops/recover_background_notices.py` into an
existing healthy-gateway check on each participating host. A pass uses explicit
local workspace and installed CLI paths, a host lock, bounded send attempts and
retry spacing; it creates no daemon and performs no remote SSH scans. The
macmini gateway-recovery watcher is the initial integration point. Other hosts
need their own existing authorized recovery entry before automatic replay is
available there. A merged producer alone does not establish fleet recovery.

Malformed receipts remain visible as errors; failed or timed-out sends retain
their intent. Acceptance clears only the intent, leaving the ordinary log ring
to reclaim output later. Pending delivery can therefore exceed the normal log
cap and needs operator attention during a sustained outage. Processes killed
before they publish a completion intent retain the existing no-notice gap.
