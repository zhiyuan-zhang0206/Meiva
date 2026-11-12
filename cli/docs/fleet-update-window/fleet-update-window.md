# Fleet update window guarantees

The promises `meiva update` makes around the stops themselves
([Fleet update over SSH](../fleet-update.md) owns the halves and their order): what
refuses the window, what gates it, what it silences, and what must not leak.
What stops and what starts is never decided here: every choice is the operator's
explicit argument, forwarded unchanged.
`run` executes both halves in one window, preserving these same gates and
closing the silence only after `up` passes all verification.

## Preflight

Each listed host is probed in its own shell before anything is touched. The
probe uses `printf` because zsh's and dash's `echo` reinterprets backslashes,
which once mangled a record carrying a quoted `$HOME`.

## The failed stop

Only after a stop fails — which hosts the window took down and must bring back
([The failed-stop recovery](failed-stop-recovery.md)).

## The kept-root gate

A window that keeps the application root (`--keep-shell-sessions` or a kept service) does not
restart `meiva-root`: the `up` half's `meiva start` finds it running and reuses it only when
NEW would request the generation it was launched with (its launch digest and every unit's manifest
digest); otherwise that start is refused (`requested services change the immutable root generation:
root launch inputs`) after every service stopped, and the cluster stays down until the root is
stopped by hand (2026-11-11, twice). So before anything stops, `down` asks every host: NEW is
checked out in a throwaway worktree (`$HOME/.meiva/pre-update-root-check`) and its own
`cli/commands/lifecycle/live_generation.py` reads the running root's status and builds the manifest
a start of NEW would request with the operator's service selection, so the verdict is the code
that would refuse, not a guess from the diff. A host whose root NEW cannot reuse refuses the
window (exit 2, nothing touched) and the message names the choice to make instead:
`--close-shell-sessions` with `--keep-no-services`, which restarts the root on the new code. The
fleet never changes the operator's choice for them. A host with no root passes; a check that
could not run warns and the window goes on (the start would say the same thing for real).
The target reader uses the host's future source checkout for its runtime environment,
including that checkout's `.venv` in PATH and VIRTUAL_ENV. The disposable reader's
import location supplies the target implementation, not the environment a future
start will launch. Those paths remain launch inputs: changing the actual runtime
or the declared service PATH still refuses reuse, as do configuration or delivered
write-generation changes.

The gate also runs under `--dry-run`, where it fetches NEW's objects on each host (nothing else:
no ref, no checkout) because its answer is NEW's. A window that stops everything has no root to
check and skips it.

## The stored-schedule gate

A stored schedule script lives in the database, not the checkout: a library
change leaves it crash-looping the moment the schedule manager starts, and
the gateway's own `meiva schedules verify` only exists after that start. So
`down` checks first, the only code that may read the table being the home's
own OLD checkout, and the check being NEW's, offline, under a throwaway home;
both steps are real files whose text ships to the host, so their imports stay
visible.

A pass leaves a receipt on the gateway (`updates/schedules-verified`, naming NEW).
A `down` rerun after a failed stop finds the gateway's database already stopped,
so the table cannot be read: for the same NEW, and only while the receipt is
younger than `--silence-hours`, the receipt stands in for the verdict and the log
says so (`REUSED`). Without a matching, fresh receipt the table is still
unevaluable and warns, continuing.

## The silence

`down` opens one Grafana silence on the gateway host before the first stop
(comment naming NEW, expiry the required `--silence-hours`) and `up` shortens it
to a 10-minute post-`up` settle window (`SETTLE_WINDOW`) after the drift checks so
rules that fired during the `down` window can complete their `keepFiringFor: 5m`
/ 6-evaluation recovery window and 5-minute health-probe cron tick before
Alertmanager unsilences them. A silence that cannot open only warns; a failed `up`
leaves the silence to its original expiry. `--dry-run` prints the command without
opening.

## The log and the macOS start

Every printed or logged line passes the secret redaction patterns first
(bearer tokens, secret-named assignments, URL passwords), and the log is
created 0600. The macOS one-shot writes its plist under a fresh 0700
directory in the remote `/tmp` and is left loaded past the start deadline,
reporting its label and log path; its label is its own
(`com.meiva.fleet-update.*`), never a registrar's job.

## Contracts

down refuses before touching anything — exit 2, no effects — when a listed host has uncommitted changes, a post-checkout hook, an active update marker, when the hosts disagree on HEAD, and when .python-version changes without --allow-python-change; the probe reads its facts through the host's own shell, among them whether the host is already stopped: its last lifecycle operation (`run/lifecycle-op.json`) is a completed stop with rc 0 and no meiva-root is running. A stop counts only when `meiva stop` exits 0 and that journal then records it complete with rc 0; a start counts when `meiva start` exits 0, which it does only after readiness.

Before anything stops — under --dry-run too — down asks every host whether the root a kept-root choice leaves running can serve NEW ([The kept-root gate](#the-kept-root-gate)); a root of another generation refuses with exit 2. Then down runs NEW's offline verify over the gateway's stored schedule table from a throwaway worktree: the read step runs on the home's OLD checkout by signature dispatch, the check goes through NEW's schedules CLI under a throwaway home, VERIFY_RC separates a crashed check from a red, a host that has not fetched NEW is reported as not checked, and a red or unevaluable table warns with the rows listed, continuing; when the rows cannot be read, a receipt of an earlier pass of the same NEW younger than --silence-hours is reused and reported instead.

After a failed stop, down recovers the hosts that stop took down: [The failed-stop recovery](failed-stop-recovery.md).

down opens one Grafana silence on the gateway host before the first stop, its comment naming NEW and its expiry the required --silence-hours; up shortens it to a 10-minute post-up settle grace after the drift checks; a silence that cannot open only warns and the half goes on; a failed up leaves the silence to its expiry; --dry-run prints the command without opening.

up starts every listed host even when one fails — except that runners are not tried when the gateway did not start, since they join through it: each is reported as a failed start saying so — a host whose start or converge aborts is reported with the step and reason recorded by that host's incomplete-converge marker ([Incomplete converge marker](../../commands/converge/docs/converge-incomplete.md)), the remaining hosts still start, and the run exits 3 (partial) after listing each failure — distinct from 2 (refused before any effect) and 1 (a failure before any host started). Verification is per host and does not stop at the first red: a red drift check and a failed package refresh are each that host's failure, the other hosts are still verified, and a host the window could not verify at all — a roster that never agrees, or a host that never started — is a failure naming why, never a silent skip. Every listed host ends with one matrix line: `serving`, `failed at <step>`, or `skipped: <why>`. A roster that cannot agree on the window's hosts is itself the failure, reported once per host. The silence is shortened to its post-up settle grace at the end of up even on a partial one: hosts that came back healthy must not spend the rest of the multi-hour window with their alerts suppressed, while each failed host is already named in the report.

## The helper gate

Before any host stops, every host is asked the permissions-helper facts of the commit the window switches to, read by that commit's own module — the installed checkout is OLD and on a first wave does not carry the command at all.

Two shapes answer. A host that carries no permissions helper (a Linux machine) owes `helper=absent` and nothing else: there is nothing there to rebuild, so it passes and is never counted among the rebuilding hosts. A host that carries one owes all six facts, and a missing, empty or `-` answer refuses the window before anything stops — reading a probe that could not run as "nothing to do" is how a window walks past the one host it should have stopped for. A host with a helper must also carry the certificate root this fleet records for it in `deploy/fleet/helper-identities.json`, keyed by the SSH alias the window names. Both `mba` and `macbook-air` record the laptop's same verified certificate root; the gate does not infer an alias from the machine name.

The finite probe words are decoded explicitly: helper presence is `present` or
`absent`, and helper validity/loading/staleness plus host
hook/active/stop_message flags are `yes` or `no`. An unknown word is malformed evidence and produces an explicit
CLI refusal. Host flags are checked before fetching; helper facts are checked
after the necessary non-destructive fetch of NEW and before stop, switch or
rebuild effects. Missing helper facts retain the unreadable-facts refusal above.

Parent: [Fleet update over SSH](../fleet-update.md).
