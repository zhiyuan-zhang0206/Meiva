# Fleet update structure

`main` and `run_parsed` are a shell around decisions. Every ambient fact reaches them as a value read once by the
entry (`cli.main`): the repository the NEW commit is resolved in, the environment (the `PATH`
that finds `ssh`, and the agent marker that refuses a run from inside an agent), the time the log
and the one-shot job names are stamped with, the pacing of its waits, and the clock those waits
are spent on. The synchronous window receives those facts as inputs. The separate scheduled worker is an OS-owned process entry and captures its own environment and clock.

- `session.py` owns the connection to the hosts: the operator's own `ssh`, found on the `PATH` the
  process was started with, and the one redacted log. `FailedError` is a step that cannot
  complete; `RefusedError` is a window that refused before touching anything (exit 2). It also
  declares the `Timers` port and carries the clock the window's waits are spent on, and the
  `deferred` ledger of runners left out of the window (`defer`, `live`, `gather_hosts`): only the
  gateway's failure ends a wave, a runner's is recorded there.
- `timers.py` is the process clock behind that port: the entry point builds one and hands it to
  the session, so the roster wait reads `Session.timers` and no module here reads `time` for
  itself.
- `choices.py` parses and validates every explicit choice. `down.py` and `up.py` are the two
  halves; `fleet_gate.py` and `fleet_verify.py` are the reads and the verification they run.
- `plan/` holds what the window decides, as strict pure modules: target mode, CalVer release
  certificate, and channel pointer verification (`release_target`), the order of the stops and the
  flags each host gets (`stop_wave`, `wave_order`), the journal reading that says whether a stop
  finished (`journal`), what a failed stop brings back (`recovery`), what the probes refuse on
  (`host_probe`, `helper_gate`, `old_target`), how the shipped programs' replies are read
  (`stored_schedules`, `silence_replies`), the roster gate and the
  verification steps (`roster`, `verify_steps`, `host_failures`), and the command text each host
  receives (`host_commands`, `gateway_commands`, `prestage`, `oneshot`). The shell reads facts,
  runs the commands and prints the lines these return.

- `scheduling/` prepares a host-owned one-shot, stores its operation record and routes
  warnings. Its entry selects the local OS timer and verifies that preparation did
  not spend the deadline. The pure `scheduling/plan.py` parses deadlines and directory
  replies. `scheduled_worker.py` claims the record under a file lock and runs one
  pinned-target attempt; cancellation takes the same lock and refuses an executing job.

## Contract

The synchronous update window receives its ambient facts from the CLI entry: main receives the repository, the environment, the stamp time, the pacing and the clock as values, every host command goes through the ssh found on the PATH main was given, and each decision the window makes is a pure function in plan/ of the text a host printed.

Parent: [Fleet update over SSH](../../docs/fleet-update.md).

## Fleet rollout evidence

After dependency pre-stage and before opening the silence or calling any stop, the
coordinator runs the target checkout's compatibility probe on each participating
host. Pure runners request a signed mirror using their installed runner API
authority and enrolled address. The target authenticates the claims, confirms that
the authorization covers the stop and start budgets, then publishes through the
bootstrap mirror writer. Only receipt metadata travels back over SSH. The gateway
uses its authoritative local startup configuration. The probe validates the target
settings and selected service roster, checks the installed harness CLIs, and reads
the applied database migration set through the host's normal credentials.

A domain receipt has a wire version, full target SHA, configuration generation
(the runner mirror generation or the local startup configuration digest), required
and applied schema versions (digests of the migration-name sets), and observations
keyed by component. A preflight receipt is stored owner-only in the host's
`updates/` directory. The fleet refuses unknown or incompatible receipts before
stop, including a mixed config generation on pure runners or a database ahead of
the target. Pending migrations require the full listed fleet with no kept named
services. A runner stop failure on such a window aborts before the gateway stop
and recovers the runners that already stopped. Stored-script verification retains
its advisory policy and logs its verdict alongside the gateway's target, config,
and schema. The schedule-manager protocol is checked independently after startup.

After start, two probes run separately. The supervision probe uses the root's
authenticated status and checks requested unit process state and launch generation.
The roster still checks live source SHA, pinned to the requested target for
`update run`. The domain probe compares the installed and serving runner config
projections, verifies the applied schema matches the target, and reads component
protocol readiness and harness CLI versions. The probe loads host-local settings
before applying the verified runner projection so roster composition sees the same
settings as target boot. It compares its observations with the
staged expectations. Harnesses absent at preflight remain optional. Process-only
units contribute to supervision. Readiness evidence identifies a service and its
configuration and schema context. The domain probe makes read-only observations
and uses the existing per-service identity probes, whose readiness semantics
belong to those services.

A retained root with an unavailable compatibility verdict refuses the window.
Full stops replace the root. Targets without the receipt program and gateways
without signed runner mirrors refuse before stop. A standalone recovery `up` can
still start a legacy checkout and reports the unavailable proofs explicitly. A dry
run reports that it skipped the publishing compatibility probe.
