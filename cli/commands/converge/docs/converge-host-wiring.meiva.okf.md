---
type: doc
title: Converge host wiring
description: The firewall, redis_bridge and hook-drift warning steps and their guard rails.
tags:
- cli
---

# Converge host wiring

Cold start passes its admitted service roster into preparation before publishing
the desired service file. Service-specific steps require a selected consumer;
host wiring and private data-plane preparation remain shared dependencies.
Standalone `meiva converge` takes the same explicit service selection as `meiva start` (`--all-services`, `--only-service` or `--disable-service`), applies the capability gates to it, and passes it to every step; the boot job registers exactly those services and an empty selection is refused.
`converge_host` is the composition point of one operation: it builds the context the
steps share from the invocation's `CommandRuntime` (its `home` is the home converged and its
`session` the invoking user's, built once in `CommandRuntime.create` as a `UserSession`: the
user's home directory, the login shell, whether a person is at the terminal), decides whether the
home is the host's prod install, and hands both to `run_converge`. Steps read the session and
the host rather than `Path.home()`, `$SHELL`, `os.getuid()` or the TTY, so a test hands in the
values it wants.
`step_selection.py` (`strict`) holds the pure decision of which steps run and why each other
step is skipped (consumer service not selected, host-global wiring off the prod install,
capability roles, unit not configured yet); `run_converge` prints the decisions and runs the
steps, records the first failure for the prod install and re-raises.
Each converge operation creates one lazy `ConfigBoot` and passes it through the
required `ConvergeCtx.config` field. Its required `producer` borrows the entry's
same lazy event-pipeline owner; LGTM render failures use that writer without
constructing another pipeline. Its required `database_factory` borrows the
entry's operator owner across all selected steps. Its required `clock` is the
invocation's `CommandRuntime.clock`: the incomplete marker, the ownership preflight log and the
health preflight stamp with it, so a test hands in the instant it wants. Its required `helper` is the
invocation's `CommandRuntime.helper` factory: the screen-capture, Accessibility and
helper bring-up steps call it only on a macOS host that can run a helper, and a
test that does not mean to reach one passes `refuse_helper`. Host capability steps and OS
job registrations read that owner's live view
at each existing step boundary, including browser, permission helpers, memory,
transfer, GitHub PR checks, the package refresh cadence and WAL-G schedule.
The WAL-G preparation and job steps read the same `view.walg.walg_config_file`;
clearing it still retires the existing job without an OS registration gate.
Creating the context does not deliver configuration or register an OS job.
The first selected config reader captures inputs already delivered by startup;
converge itself does not repeat dotenv delivery or replace process environment.
PostgreSQL runtime admission, WAL-G's remote-plane refusal and health-preflight
URLs read this same configuration owner at their original step boundaries.
Unconfigured health checks and disabled WAL-G retain their short circuits.
Health findings remain warning-only; invalid WAL-G and local-runtime inputs
retain their existing startup errors.
Collector, browser and frontend preparation skip unselected services. Native
LGTM downloads only selected backends, and invokes Loki's config validator only
when Loki is selected. Readiness still checks every selected service.

PostgreSQL preparation follows runtime binary resolution: an existing vendored
tree remains selected; otherwise a complete installed PostgreSQL 17 toolchain
and its pgvector files are validated without downloads. The installed tools and
`pg_config` must describe the same installation; pgvector must include the base
install script named by its control file's default version. Missing tools, another major,
or missing extension files fail before data-plane startup. Only an absent
installation triggers the pinned vendored distribution. Linux arm64 can use its
native PostgreSQL 17 packages without requiring an unrelated download artifact.
Remote-managed data planes skip local server and extension preparation entirely.

- `firewall.py` reconciles the per-binary Application Firewall manifest.
  Version-stamped Python, Postgres, Homebrew, browser, and observability paths mean
  an upgrade can orphan the old ALF identity while loopback keeps working — issue
  #949. The step adds and unblocks resolved manifest paths, then removes stale
  managed rules. These `socketfilterfw` mutations were empirically verified without
  elevation on the macmini running macOS 15.3.1; other versions fall back to
  `sudo -n` and then an exact manual command without blocking `meiva start`.
  See
  [[base/docs/base.meiva.okf.md|Base Library]].
- `steps.ensure_local_git_hooks` warns when any conventional local
  checkout's Git hook installation is missing or drifted (via
  `base/deploy/git/check_git_hooks.py --scan-machine`, run through `ctx.host` from
  `ctx.repo`); warn-only, it never blocks a start or update. `checkout_warnings.py`
  decides the argv and which output lines are warnings. See the runbook's Git hooks
  section.
- The rest of the decisions live in `strict` pure modules beside their steps and the shells
  only execute them: `shell_rc.py` (the `~/.local/bin` PATH block of the user's rc file),
  `browser_plan.py` and `helper_plan.py` (what the browser and permissions-helper steps do
  for the host's incapability reason), `split_host_checks.py` (the transfer and GitHub PR
  checks apply only to a runner split from its gateway), `walg_plan.py` (the WAL-G steps, in
  order, or a refusal), `ownership_plan.py` and `firewall_report.py` (what the ownership
  preflight and the firewall step and verbs tell the operator).
- `redis_bridge.py` retires the standalone launchd job and installed relay copy an
  earlier layout left (the relay now runs as the root-supervised `redis-bridge` unit)
  and exposes the authenticated Redis PING used by `meiva status` and the alert-only
  cluster health check (`probe_redis_bridge` takes the home and the host whose platform
  decides whether a relay is needed). Redis itself never widens beyond loopback.
