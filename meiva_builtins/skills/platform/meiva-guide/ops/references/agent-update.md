# Agent-initiated cluster updates

An authorized Meiva operator agent can submit an entire fleet update from its
own `meiva.shell.run(...)` shell. On macOS, submit a one-shot job to launchd:
launchd owns the updater, so the submitting shell, agent and `pty-sessions`
service can stop while the update continues. This is a submission recipe, not a
new daemon or a Meiva schedule. A cluster-owned schedule stops with the cluster.

## Prepare the operation

Use `python -m cli.fleet_update run --help` from a separate operator checkout
that already contains `run`, with its own real `.venv`. Keep that checkout and
venv unchanged until the job finishes; do not execute from a unit's production
`$HOME/.meiva/source`, which the update switches. The command targets each SSH
user's `$HOME/.meiva` and requires key-based non-interactive SSH to every listed
host, including itself if the submitting Mac participates in the cluster.

Confirm the authorized target commit and complete participating host list, CI,
review and recovery evidence as for any rollout. One operator owns the window;
do not submit another update while one is running. Every participating Mac needs
a logged-in GUI user for its existing start procedure. This LaunchAgent survives
PTY/service restarts, not user logout or machine reboot.

`run` performs preflight, `down`, then `up` and all verification in one process.
Use it when no manual release work is needed between the halves. For an update
requiring such work, use separate `down` and `up` jobs and follow the release
procedure. `run --dry-run` performs the existing read-only preflight and prints
both halves' effects; it is not a successful deployment.

## Submit from the agent's shell

Replace the checkout, fixed target SHA and SSH aliases below. Repeat runner
aliases as needed, or omit them for a gateway-only roster. The operator checkout
must already contain the target commit. The Python below uses only the standard
library; it generates a private job directory, runner and plist without importing
Meiva or copying the agent's environment into the job.

```bash
set -e
OPERATOR_CHECKOUT=/absolute/path/to/operator-checkout
UPDATE_DIR="$(mktemp -d "$HOME/.meiva-fleet-update.XXXXXX")"
UPDATE_LABEL="com.meiva.fleet-operator.$(uuidgen)"

"$OPERATOR_CHECKOUT/.venv/bin/python" - \
  "$OPERATOR_CHECKOUT" "$UPDATE_DIR" "$UPDATE_LABEL" \
  NEW_SHA GATEWAY RUNNER_1 RUNNER_2 <<'PY'
import plistlib
import sys
from pathlib import Path

checkout, directory, label, target, gateway, *runners = sys.argv[1:]
root = Path(directory)
command = [
    str(Path(checkout) / ".venv/bin/python"), "-m", "cli.fleet_update", "run",
    "--new", target, "--gateway", gateway, "--log-dir", str(root / "logs"),
]
for runner in runners:
    command.extend(["--runner", runner])
driver = root / "run.sh"
driver.write_text('''#!/bin/bash
result=$1
shift
"$@"
rc=$?
printf '%s\\n' "$rc" > "$result.tmp" && mv "$result.tmp" "$result"
exit "$rc"
''')
job = {
    "Label": label,
    "ProgramArguments": ["/bin/bash", str(driver), str(root / "exit-code"), *command],
    "WorkingDirectory": checkout,
    "EnvironmentVariables": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
    "RunAtLoad": True,
    "KeepAlive": False,
    "StandardOutPath": str(root / "stdout.log"),
    "StandardErrorPath": str(root / "stderr.log"),
}
(root / "job.plist").write_bytes(plistlib.dumps(job))
(root / "label").write_text(label + "\n")
print(f"Update receipt directory: {root}", flush=True)
PY

launchctl bootstrap "gui/$(id -u)" "$UPDATE_DIR/job.plist"
```

Keep the printed receipt directory in the task's durable context. A successful
`bootstrap` means submitted, not updated. Return from the shell tool and end the
current turn so the agent can reach its drain boundary; do not wait in the tool
for the update to finish. All remaining update work belongs to the job. Do not
strip `MEIVA_AGENT_ID` in the hosted shell to evade the fleet command's refusal,
or substitute `nohup`/`&` for OS ownership.

## Observe and recover

After the agent resumes, or from an external login shell while the cluster is
down, substitute the saved directory and read:

```bash
UPDATE_DIR=/absolute/path/to/saved/receipt-directory
launchctl print "gui/$(id -u)/$(cat "$UPDATE_DIR/label")"
cat "$UPDATE_DIR/exit-code"
cat "$UPDATE_DIR/stdout.log" "$UPDATE_DIR/stderr.log"
```

The command's redacted phase log is under `logs/`. Exit code `0` from a real
`run` means both halves and the existing roster, agent smoke and drift checks
passed. Report those results and any unlisted/offline machines. Missing
`exit-code` means running or interrupted, never success; inspect the launchd
state and logs before deciding which, and keep external SSH access for recovery
when the agent cannot resume.

The first failure stops the job; it does not roll back or automatically retry.
The log names the failed `down` or `up` phase. Fix the cause and rerun that half
through an external job/login shell. After repairing and completing `down`,
run `up`; an `up` failure needs only `up`, not another outage. Follow the
runbook and `docs/conventions/operations/graceful-maintenance.md` for incomplete
stops or maintenance holds. Do not blindly resubmit the full `run`.

Once the job has finished and its result is recorded, unload its own label with
`launchctl bootout "gui/$(id -u)/$(cat "$UPDATE_DIR/label")"`. Retain the receipt
and logs needed for diagnosis; release the operator checkout only after the job
has no live processes.
