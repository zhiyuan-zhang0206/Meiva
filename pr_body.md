Every generated OS job now declares what it promises: the heartbeat file
it writes when it finishes, the owner who is called when that file stops
moving, and how late it may be. The rendered plist or crontab line
carries it, and the definition refuses to render a job that declares
nothing.

Why this and not a checklist: a launchd plist that writes nothing is
indistinguishable from one that was removed.
`com.meiva.packages-refresh` has consumers (the package pull path reads
what it writes) and nobody watching — stopped for two days it shows up
only as versions that look old. `logs-maintenance` and `pr-flow` are the
same shape, and `walg` is the sample where a declaration exists and no
plist does: on company-mini the tick is uninstalled, which nobody could
see before because nothing stated it should be there.

**One channel, one format.** The heartbeat is `{"schema": 1, "name":
..., "pid": >0, "updated_at": <epoch seconds>, "ok": bool, "rc": int}`,
written to `<MEIVA_HOME>/state/heartbeats/<name>.json` — inside the
home, never an agent workspace. That directory is already in the
liveness census's `heartbeat_dirs`, so no reader changed: the field
names and the instant are the ones `guard_health.py` and
`sre/liveness-census.py` parse today (epoch seconds, not ISO-8601;
`name` equal to the file stem; a positive pid). The population side is
one JSON document, `meiva jobs heartbeats`, so a reader outside this
checkout can compute both directions of the diff — declared but not
installed, installed but not declared — without importing this
repository. It exits non-zero instead of printing an empty set: "this
machine expects nothing" and "the declarations could not be read" must
not look alike.

**Who writes it.** Not a wrapper script: the rendered definition carries
`MEIVA_JOB_HEARTBEAT`, the absolute path of the file this run must
write, and the command writes it on the way out — the CLI entry for
three of them, the PR-flow script for the fourth, since that job runs
the exporter directly. Every exit writes, success and failure alike,
with `ok = (rc == 0)`: recording only success would fold "ran and keeps
failing" into "did not run", and those two have opposite fixes. A write
that fails exits 3, never the command's own code, because a run must not
report a success it could not prove. The recorded pid is the process
that did the work, not a wrapper's.

**Two refusals at render time, not in review.** A job whose declaration
is missing does not render, and two jobs declaring one heartbeat name on
one machine do not render — one file would answer for both, so a dead
job would keep the other's heartbeat fresh. Both name the job in the
error. The four `*_content.py` files are the whole population and all
four are declared here, so there is no half-state where some jobs still
render without one; a test walks the directory for `*_content.py` files
and fails when a fifth module declares nothing.

**The exception form.** `heartbeat: none` is the only way out, and it
carries a reason, an owner and an `until`; any of the three missing
refuses the render. Expiry does not: seven days before, the artifact
reports `none-expiring:<job> in <k> days`; after, it renders as usual
and reports `none=0 expired=<n>` with the job, its owner and its reason.
Refusing at expiry would fail a converge on the day the exception lapses
and hand that failure to whoever is deploying, who is usually not the
owner. Each artifact also states the whole population's counts —
`declared=4 heartbeat=4 none=0 expired=0` — so a job that drops out of
the declaration set is visible without diffing four documents.

Both `*_content.py` and the plists they render take their cadence from
the same `HOUR`/`MINUTE`/tick values, so a declaration cannot describe a
schedule the scheduler was never told. The declaration's tolerance is
named `late_after_seconds` rather than a "grace": nothing in this
repository waits on it — it is handed to a reader elsewhere that
compares it with its own clock — and a name the clock lattice owns would
read as a timer this process starts.

Not in this change: the alarm outside the machine. The census and
`guard_health` can now judge these four jobs, but no Grafana rule
watches the new series yet; until one lands this is "judgeable inside
the machine", not "someone is watching". Aligned against the readers as
of 2026-11-08: schema 1 with the six fields above.

Verification: `base/host/system` (the four renderers' rendered-artifact
assertions and the refusals), `cli/commands/jobs/tests` and
`services/desktop/permissions_helper/tests` — 354 passed. `python -m
meizel check` clean; basedpyright 0 errors on the changed files; the
pre-commit and pre-push hooks pass. Two local failures predate this
change and reproduce on the base commit: `test_walg_cmd.py` (two cases
reading a real PostgreSQL receipt on this macOS host) and
`test_pg_runtime_converge.py` (no local pgvector for the pg17 runtime).

Closes #6508.

Co-authored-by: Meiva #7477
