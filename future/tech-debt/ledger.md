# Tech-debt ledger

> Living tech-debt ledger. Current inspection rules and automated owners live in
> [technical-debt guidance](../../docs/conventions/engineering/tech-debt.md).
> The single "what debt is open now" view. Forward-looking (what we intend to
> fix) → lives in `future/`. Entries are agent + human maintained: humans
> set `wontfix`, and the sweeper must never re-add a `wontfix` item. Resolved
> items are deleted, not archived here — resolution history lives in git.

This is the single register for debt from every mechanism; do not create a
parallel ledger. A new mechanism, including a cap-domain "exit" registration,
records an ordinary fingerprinted entry here with its own class. PRs that
knowingly introduce debt register it here through the PR template, and a
requested reconciliation checks this ledger.

<!-- watermark: last-swept-sha=aa53f847e last-swept-date=2026-10-19 -->

## Entry format

Each open or wontfix item is one `###` block keyed by a stable fingerprint, so a
reconcile pass can match it across runs:

    ### <fingerprint>
    - **class**: deps | docs-aging | fail-fast | inline-marker | dead-code | boundary | skill-desc | docstring-budget | locality
    - **status**: open | wontfix
    - **evidence**: file:line refs / command output; for `boundary`, the named
      files+symbols and one line on why it is a smell
    - **first-seen**: YYYY-MM-DD (PR #NNN)
    - **last-verified**: YYYY-MM-DD

Fingerprint convention: `<class>:<file>:<symbol>` for localized classes; a short
human-readable `<class>:<slug>` for `boundary` (spans files, no single symbol).
Mechanisms may add structured bullet fields beyond this base set (for example,
cap-domain exits use `exitType`, `expires`, and `approver`); readers ignore unknown fields.

## Retired guarantees

The PTY whole-tree disappearance/proof requirement is retired by the user
[decision of 2026-10-07](../../docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md).
Do not reopen it as an implementation defect or infer it was fixed by passing
reruns. Historical Bash/TERM and OS-stall observations remain evidence for
operational investigation; this decision does not establish their root causes.
The job-readiness and interactive-write entries below retain their narrower
fixture/incident scope.

## Open

### boundary:base-lm-harness-claude-private-transport-pid
- **class**: boundary
- **status**: open
- **evidence**: `base/lm/harness/process.py:client_pid` reads the SDK client's private transport (`_transport._process.pid`) to obtain the CLI pid, because `claude-agent-sdk` 0.2.168 exposes no public accessor; the bridge is the caller that must observe the process tree to assert a cancel. The code degrades to `None` when the shape changes (a cancel then reports `settled=None`, never a false pass), but a 0.x SDK bump can silently disable the assertion until the attribute is re-checked. Repair: move to a public SDK accessor when one exists, or assert the attribute shape in the bridge's real end-to-end run (`demos/claude-sdk-bridge/bridge_demo.py` already fails on `settled is not True`).
- **first-seen**: 2026-10-19 (PR #6358)
- **last-verified**: 2026-10-19

### boundary:harness-registration-two-owners
- **class**: boundary
- **status**: open
- **evidence**: `base/lm/harness/registration.py:HarnessRegistration` is the writer the bridge uses; `meiva/impersonation/harness/registration.py:FileRegistrationStore` is the reader the attached child uses. Both decode the same `$MEIVA_HOME/harness/registrations/<agent_id>.json` document and both name the field set and the `attached` status, because the SDK surface sits above `base` and cannot be imported downward. The coupling already bit once: the bridge wrote `status="active"` against a reader that requires `attached`, which would have refused every child. Repair: one dataclass for the document (owned by `base`, re-exported by the SDK module) plus a parity test, once #6387 lands.
- **first-seen**: 2026-10-19 (PR #6393)
- **last-verified**: 2026-10-19

### boundary:supervisor-intent-fixture-stop-window
- **class**: boundary
- **status**: open
- **evidence**: Re-verified 2026-10-19 at main@aa53f847. The fixture coupling this entry recorded is still repaired: `services/supervision/meiva_root/tests/test_meiva_root_intent.py:67:_supervisor` builds `SupervisorConfig(stop_timeout_s=0.2)`, and `:73:_up_failure_supervisor` SIGKILLs and awaits the captured disposable sleeper before delegating to the unchanged `_stop_unit`, so the up-half/episode tests never enter a live TERM stop window; input-seal failure, durable intent, episode clearing and fresh generation identity remain exercised. The stop path itself has been rewritten since this entry was written: `stopping.py:_stop_posix_generation` no longer exists, and `services/supervision/meiva_root/stopping.py:72:_stop_generation` now runs TERM → bounded graceful wait → one SIGKILL escalation → reap (PR #6198), so the tree-capture / birth-recheck description in the previous evidence describes code that is gone. `test_meiva_root_stop_window.py` retains real TERM/deadline coverage. The open item is unchanged: attempt 1 of PR #4321 (run 37318714866, shard 10) overran the 0.2s window on leader PID 10633, and the trace does not establish that process's state after tree capture and durable custody I/O at the deadline; no repro and no CPU-load or signal-delivery evidence exists. Diagnose that incident before changing `stop_timeout_s` or claiming its root cause resolved.
- **first-seen**: 2026-10-05 (PR #4321)
- **last-verified**: 2026-10-19


### boundary:pty-kill-verdict-job-readiness
- **class**: boundary
- **status**: open
- **evidence**: Re-verified 2026-10-19 at main@aa53f847. `base/sessions/tests/test_pty_backend.py:251:test_kill_session_with_verdict_reports_whether_work_was_cut_short` waits for the exact job (`wait_for_job(shell_process(...), ["sleep", "300"])` at `:264`, imported from `base/sessions/pty/tests/job_wait.py:wait_for_job` at `:33`), so the missing intended-job precondition stays repaired and the real kill/verdict assertion is unchanged. The cross-reference in the previous evidence is stale: `services/agent_runner/pty_sessions/tests/test_sessions.py::test_kill_a_session_with_work_reports_interrupted` was removed by 466219fc4 ("Simplify PTY closure to bounded best effort", 2026-10-06) together with the retired whole-tree proof, so the backend test is now the only kill/verdict case. The 2026-10-06 first-attempt failure (verdict `(True, "forced", False)` on a child whose identity was never captured) still has no proven cause: do not infer a production membership defect or close the Bash/TERM incidents from this.
- **first-seen**: 2026-10-06 (PR #4356)
- **last-verified**: 2026-10-19


### boundary:interactive-bash-smoke-first-write
- **class**: boundary
- **status**: open
- **evidence**: Re-verified 2026-10-19 at main@aa53f847. The test and its arrangement are unchanged: `meiva_builtins/skills/platform/meiva-guide/external-agents/tests/test_impersonation_launch.py:152:test_app_server_command_executes_in_an_interactive_bash` still writes the export-PATH line and then the app-server command through `_start_pane` (`:134`), which uses `pty.openpty` plus `Popen(start_new_session=True)`, while production `services/agent_runner/pty_sessions/session.py:82:fork_shell` uses `pty.fork`; that difference remains the only investigation lead and is not a proven cause. The recorded observations stand: the macOS first `os.write` blocked, the unchanged main node exceeded a 65-second bound with a fresh temporary `MEIVA_HOME`, and a 25-second faulthandler run located that write; a fresh macOS exact-node run passed on 2026-10-06 and a native probe disproved missing controlling-terminal ownership at the shell prompt, neither of which explains the hang. The Linux CI check remains selected. Do not infer an app-server or production transport defect, add a timeout, or skip the check from this evidence.
- **first-seen**: 2026-10-05 (literal coding-session refresh)
- **last-verified**: 2026-10-19


### boundary:exec-node-runner-test-seam
- **class**: boundary
- **status**: open
- **evidence**: PR #6233 shipped the exec-family telemetry attribution (`exec` / `exec_failed` records carrying `agent_id`; the emit site is `agent/graph/exec/node.py:399`) with the llm-side injected test only (`agent/graph/llm/tests/test_turn_end_attribution.py`); its body records why the exec-side case was omitted — injecting a fake runner means monkeypatching `agent.graph.exec.node.run_agent_code` around the private `_exec_node_impl`, and `agent/graph/exec/tests/meizel.toml:16 [debt.first-party-patch]` ratchets every existing patch site (`test_cross_agent_findings.py:68`, `test_exec_attach_drain.py:95,136`, ...), so a new one may not be added. The PR states the fix and the priority: "Tech debt (P3, 405's ruling): the exec path's injected test needs a public seam for the runner ... a seam is its own change." Not registered in this ledger at merge time.
- **first-seen**: 2026-10-18 (PR #6233)
- **last-verified**: 2026-10-19


### boundary:notices-audit-sql
- **class**: boundary
- **status**: open
- **evidence**: `cli/commands/agents/notices_audit.py:33` holds the one SELECT left behind `meiva notices audit` — `agent_notices` rows that need a response — executed at `:93` against a psycopg cursor. The other two statements (halts for the removed `core.turn_finished`, `agent_tasks` for the removed `meiva_fleet.task_closed`) were deleted with those categories on 2026-10-19, which reduces but does not close the gap: `cli/commands/agents/tests/test_notices_audit.py` exercises only the in-memory candidate and replay path; nothing reaches the query text, because unit CI carries no database. Both PRs that introduced it say so in their bodies: #6229 ("the SQL has no automated test (no database in unit CI); it was exercised against the live runtime database") and #6238 ("Not covered: the SQL still has no automated test"), and #6238 added `--explain` on top of it. Column renames and policy changes break this query silently.
- **first-seen**: 2026-10-18 (PRs #6229, #6238)
- **last-verified**: 2026-10-19


### deps:playwright-1.62to1.64
- **class**: deps
- **status**: open
- **evidence**: `uv pip list --outdated` (worktree venv at main@aa53f847, 2026-10-19) reports playwright 1.62.0 -> 1.64.0, and PyPI's JSON API confirms 1.64.0 is the latest release; `uv.lock` pins 1.62.0. Chromium follows the wheel — `.github/workflows/ci.yml:2004` installs it with `uv run playwright install chromium` and `cache-warm.yml` warms the same `~/.cache/ms-playwright` cache keyed on the `uv.lock` hash — and no `PLAYWRIGHT_VERSION`, Dockerfile pin or npm playwright dependency remains, so the bump is the wheel alone.
- **first-seen**: 2026-09-21
- **last-verified**: 2026-10-19


### docs-aging:agent/startup/__init__.py:wrap_saver_writes_with_nstep_interval
- **class**: docs-aging
- **status**: open
- **evidence**: the docstring at `agent/startup/__init__.py:219` says a delta fold "within one checkpoint ... sorts by opaque task-id hash". Since checkpoint-postgres 3.1.3 (PR #6295) the fold orders one checkpoint's writes by `writes_sort_key` = (`task_path`, `task_id`, `idx`), and `base/agents/history/checkpoint_postgres_walks.py` uses that order for its reset cut. The conclusion the sentence supports (a skipped or re-homed batch scrambles the replay) still holds, so this is wording, not a behaviour defect. Not fixed in #6295: the docstring is unchanged legacy prose, and any edit to it trips the `check-code-comments` free-comment gate, which would force the whole 50-line explanation into `meiva:note` blocks.
- **first-seen**: 2026-10-19 (PR #6295)
- **last-verified**: 2026-10-19

### docs-aging:future/infra/data/checkpoint-storage-rebuild.md:appendix-a-drill-assets
- **class**: docs-aging
- **status**: open
- **evidence**: Appendix A of `future/infra/data/checkpoint-storage-rebuild.md` records the pre-cutover drill suite — behavioral matrix, crash replay, production-clone fingerprint subset — as living "with the author (#6095)", never in the repository. `~/.meiva/workspaces/6095` no longer exists on macmini and the 3180 workspace is empty, so no later version move can rerun that gate as specified: the 3.1.3 move (PR #6295) could only be checked by a reconstructed behavioural matrix. The doc now carries a dated note; the remaining repair is to keep verification assets with the decision that defines them (or name a durable location) the next time such a gate is written.
- **first-seen**: 2026-10-19 (PR #6295)
- **last-verified**: 2026-10-19

### inline-marker:scripts/lint/host_platform_marks.py:baseline
- **class**: inline-marker
- **status**: open
- **evidence**: `scripts/lint/baseline/host_platform_marks.json` freezes 84 unmarked host reads in 49 test files that predate `lint-host-platform-marks` (`sys.platform` / `Host.system()` / `Platform.current()` reads and ambient `shutil.which` lookups). The baseline only shrinks: a file may go under its count, never over, `--write-baseline` lowers it after a repair, and the file plus this entry go when it empties. The macOS-only `services/desktop/permissions_helper/tests/` and the tool-availability `shutil.which(...) is None` skips are the bulk of it.
- **first-seen**: 2026-10-21 (PR #6635)
- **last-verified**: 2026-10-21

## Wontfix

### docstring-budget:meiva/security.py:module
- **class**: docstring-budget
- **status**: wontfix
- **evidence**: module docstring trimmed 15 -> 6 lines this pass (soft cap 2). Kept: the module is off the default prompt surface (not in `__all_for_meiva__`, not in the SDK-expand list), so the standing prompt pays nothing; the residue is the purpose + the mitigation caveat, for `help()` drill-down readers. Cutting further would drop the non-boundary warning.
- **first-seen**: 2026-09-23
- **last-verified**: 2026-09-23

### docstring-budget:meiva/ui.py:serve
- **class**: docstring-budget
- **status**: wontfix
- **evidence**: trimmed 31 -> 20 lines this pass (soft cap 12). The residue is contract: five Args each carrying a real constraint (dir resolution, name charset, port range/policy, title/ttl defaults) plus the session life-cycle and `index.html` requirements — the class's Args-format-heavy allowance.
- **first-seen**: 2026-09-23
- **last-verified**: 2026-09-23

### docstring-budget:meiva/watcher/__init__.py:cron
- **class**: docstring-budget
- **status**: wontfix
- **evidence**: ~18 lines (soft cap 12) after the 2026-09-27 watcher-registry removal (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md) dropped the old supersede/replace/reuse residue entirely. Residue now: five Args (cron format, timezone, end_time types, name, notify) + the one-line warning that re-registering the same schedule does NOT dedupe any more — it starts a second, independent session, whose loss would silently reintroduce the double-firing confusion the old dedupe used to prevent. The class's calibration note already expected `watcher.cron` as an Args-format-heavy standing item.
- **first-seen**: 2026-09-23
- **last-verified**: 2026-09-27
