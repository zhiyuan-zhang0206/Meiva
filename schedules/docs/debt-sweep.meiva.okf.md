---
type: doc
title: Daily debt sweep
description: The mechanical scan, the clearing worker's prompt, the event and the failure report of the daily tech-debt sweep.
tags:
- schedules
- telemetry
---

# Daily debt sweep

The schedule fires at 06:30 cluster time. Each claimed slot runs the repository's mechanical
scan, keeps its report, wakes one clearing worker with the report as evidence and emits one
`debt_sweep_daily` event. The worker, not the schedule, reconciles `future/tech-debt/ledger.md`
and delivers at most one `chore(sweeper): debt reconcile <day>` PR.

## The scan

The scan is `scripts/audit/tech_debt_candidates.sh --repo <repo>` run by bash from the repository
the installed `base` package sits in, bounded at 15 minutes; on expiry the whole process tree is
killed. The report is the program's stdout then stderr, with a last line naming a non-zero
exit. A scan that cannot run at all (no bash, a timeout) is a failed scan whose report is the
error. `schedules/plans/debt_sweep_scan.py` decides the report, its one-line summary, the day
label (the slot's date in the cluster timezone, which can differ from its UTC date) and where the
report is stored: `<home>/debt-sweeps/<day>/mechanical-scan-<slot UTC stamp>.txt`.
`schedules/jobs/debt_sweep.py` runs the program and writes the file.

## The worker and the event

The worker is always dispatched, whether the scan succeeded or failed: the prompt carries the
report path and summary and tells it to re-check the findings. The label comes from
`MEIVA_DEBT_SWEEP_AGENT_LABEL` (default `debt-sweep-daily`); `ensure_agent` resurrects a
terminated agent with that label, messages a live one and spawns a missing one. The prompt, the
event payload (the declared `debt_sweep_daily` keys) and the failure message are decided in
`schedules/plans/debt_sweep_worker.py`.

## Failure

A failure anywhere in the sweep is printed and sent to the P0 lead, resolved from
`MEIVA_DEBT_SWEEP_REPORT_AGENT` or else the report label. The slot stays claimed; nothing
retries it, and the message tells the lead to run the debt-clearing pass by hand. A lead who
cannot be reached makes the callback raise, after printing why.

## Dry run

`python debt-sweep-daily-schedule.py --once --dry-run [--repo PATH]` runs the scan and prints the
report path, summary, resolved worker label, prompt and event payload a real run would produce,
with no claim, no agent call, no telemetry and no database. The two flags go together, and
`--repo` is accepted only with them.

## Evidence

`schedules/tests/debt_sweep/test_debt_sweep_plans.py` asserts the decisions on values.
`test_debt_sweep_shell.py` runs the scan as a real bash program against a small repository under
`tmp_path`, dispatches the worker and messages the lead through a gateway on a loopback port,
claims the slot in the throwaway Postgres, and runs the script itself for the dry run.
