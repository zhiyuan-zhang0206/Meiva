---
type: doc
title: CLI entries
description: Fresh executable logging and embedded command dispatch.
tags:
- gateway
- tool
---

# CLI entries

- `cli/main.py:entry()` — fresh executable logger preparation; `main()` — embeddable dispatch preserving caller sinks, which runs `run_command(argv, retained_children=, producer_factory=)` with the operator event pipeline as the producer factory (a caller that needs another producer, such as a different flush interval, calls `run_command` with its own factory); `cli.parsers` — `build_parser` for construction, `parse_args` for syntax, `command_options` for documentation.
- `cli/init_intent.py:run_init()` — the first-start inputs; `cli/start_intent.py:run_start()` — start admission (`admitted_home`) and the full home lifecycle lock; `cli/start_identity.py` — durable initialization journal and the start gate (`require_initialized`); `cli/startup/` — what init and start decide as values (sink names, the host tool directories a home declares, the settings-failure template, what a finished start owes the boot unit and the boot scheduler); `cli/commands/cluster/unit_join.py` — a runner's gateway join.
- `cli/commands/cluster/home.py` — `destroy` (confirmed at a terminal), exact cleanup before marking the home detached; `start.py` / `status.py` / `data_plane/cluster_instance.py` — runtime operations.


The console script and both Python module entry paths invoke `entry()` once.
It calls `base.log.prepare_process_logging()` before dispatch loads configuration.
That operation deliberately removes sinks only for a fresh executable process.
Ordinary imports and embedded `main()` calls preserve caller logging; repeated
concrete logger initialization retains its original first-role guard.

`init` dispatches without configuration imports. `start` checks
`require_initialized` before constructing its root, then checks the admitted home
again under its lifecycle lock. `main()` is the composition root of both: after the
checkout gate it reads the home and the checkout once into a `HomeSite`
(`cli/start_runtime.py`) and, for `init`, the caller's host facts (`CallerHost`: its
tool directories, which ports of the fixed table something already holds and the transport key
of a database-capability bundle, which leaves the environment there before any step runs), and
hands them to the handler. `run_init` and `run_start` take those values; neither reads
the environment, `__file__` or a port, and neither repeats the checkout gate. A test
therefore creates a real home and checkout under `tmp_path` and passes them. The faults it
injects are ones a disk can produce: a lock file that is a directory makes the `.env`
publication fail after the intent was written, as a power loss between the two would.

`start_intent.admitted_home` is the settings-free half of a start. It refuses a home that
cannot start before anything is created, holds the home's `start-intent.lock` and enters
the process into the home (a delivered login and its generation are discarded together);
nothing in it imports runtime Settings. `start_once` then admits the start's `StartRuntime`
under that lock (`StartRuntime.admit` verifies the checkout this process loaded and holds
its `LoadedRuntimeIdentity`; a checkout that is not that one is refused before any sink
opens), opens the start's loguru sinks, which build Settings, and runs the lifecycle inside it. What the lifecycle's exit code owes
the boot unit (`HandOffRoot` after a complete success; a failed hand-off withdraws the
serving marker before it is refused) and what a boot-job attempt tells its scheduler (done,
retry with the start's own code, or give up after the last attempt of the budget) are
decided in `cli/startup/outcome.py`; `cli/startup/log_sinks.py` names the sink of each verb
that opens one, `cli/startup/service_path.py` chooses the tool directories a new home
declares, and `cli/startup/settings_failure.py` words an unconfigured home's Settings
failure. The shells read the files, print and execute what those return. Other parsed runtime commands create one
`cli.database.runtime.CommandRuntime` with a zero-read `ConfigBoot` and its lazy
`MachineIdentity`. Its `home` is the `resolve_meiva_home().resolve()` that `main()` reads once and
hands to `create(home)`, the same value and spelling as the `HomeSite` of init and start; `create`
only holds it, and `main()` is where the private home directory is created (a symlinked home is
refused there) before the runtime is built. Its `session` (`UserSession`: the user's home
directory, the login shell, whether a person is at the terminal), its `checkout` (the checkout
whose code runs) and its `terminal` (the three process streams) are read once in `create` too, so
a command never reads `Path.home()`, `$SHELL`, `repo_root()` or `sys.stdin`. The transport key
of a database-capability bundle is the one environment value `main()` takes out for a verb
(`meiva init` and `meiva cluster db-authority install-unit`, as `args.transport_key`). Dispatch, embedded
lifecycle legs, logging, database host resolution and telemetry borrow that same
identity, and with `CommandRuntime.helper`, the one place the real permissions-helper
factory is chosen: `base.sessions.helperproc.helper_for(host.platform)` returns
`helper_client` on macOS, which reads the helper's port only when a code path calls it, and `None`
on Linux. `None` means only "this platform has no helper" and is never a switch; macOS always has
one. `start`, `stop` and `converge` hand that value down to the root driver and the helper
bring-up (a `None` takes the direct-spawn path); nothing below them builds a helper client. Successful setup
resets only this owner before role and intent checks;
failed setup preserves its cached inputs. Help exits before constructing the root,
and lightweight status/stop paths do not force identity or configuration fetches.
