---
type: doc
title: Merge gate
description: "`scripts/ci/pull_requests/gate/merge_gate.py` — the five conditions a pull request must meet before this lane merges it, the grant it reads, and why every unreadable input is a refusal."
tags:
- scripts
- ci
---

# Merge gate

`merge_gate.py <PR number|url>` prints the evidence for each condition and one
verdict line; exit 0 is GO, exit 1 is NO-GO. It is the mechanical form of the
lane's merge rule, and it fails closed: an unreadable check, a missing grant or a
truncated comparison refuses the merge rather than passing it.

| # | Condition | Why it exists |
|---|---|---|
| 1 | The body pins `Preflight on head <headRefOid>:` and carries `PREFLIGHT PASS` | A preflight result belongs to one commit; a body that still pins an older head proves nothing about this one |
| 2 | Every check run is green | Trunk queue rows are skipped (the queue is retired); one name keeps only its latest run by `started_at`, so an older cancelled duplicate cannot block a head a newer run passed |
| 3 | The pull request and current main touch disjoint files | An empty intersection makes the merge trivially clean |
| 4 | The body carries a where_used line and a behaviour section | Both are lane conventions the reviewer would otherwise check by hand |
| 5 | The trailers name a granted agent | The ordinary pyright pull requests are the user's own Claude Code line and are not this lane's to merge, however green they look |

## The grant is data, not code

Condition 5 reads a grant file: one agent id per line, `#` comments allowed,
refreshed with the query recorded in that file. `MEIVA_LANE_FILE` names it
(default `lane-agents.txt` beside the module); `MEIVA_LANE_AGENTS` overrides it
with a comma list for a one-off run. The criterion is "405 spawned this agent",
not a branch name.

The grant is not committed here. It changes as workers are spawned, so it belongs
beside the operator who runs the gate, next to the `gh` wrapper that invokes it,
not in a pull request that would go stale between spawns.

A missing or empty grant is an error. It used to fall back to a hard-coded pair
of ids, which let two machines running the same gate disagree without saying so:
one refused agents the other accepted. A grant nobody wrote cannot authorise a
merge.

The handed-over ladder is the one branch that may carry a harness trailer
(`Co-Authored-By: Claude ...`, `Co-Authored-By: Codex ...`) without a Meiva one:
the coordinator handed that whole line over, so its commits have that shape by
construction. `MEIVA_HANDED_OVER_PREFIX` sets the branch prefix; the residual
risk is a pull request opened by the user's own session on that same prefix.

## Two records the gate reads in the body

A squash merge keeps the pull-request body and drops the head commit message, and
this repository records authorship and the preflight result in the body. The
trailer scan therefore reads the commit messages and the body together: scanning
commits alone reported "no trailer" for a pull request whose body ended with one.
Widening what counts cannot refuse a lane the gate used to accept.

## The one overlap condition 3 allows

Every "make one prefix strict" pull request touches the owed pyright-test list
(one line) and the generated `pyproject.toml` region (a few lines) by
construction. Requiring an empty intersection would serialise the whole queue
behind one CI rerun each, so that pair is allowed and the gate says so. At prefix
distance 2 or more a three-way merge of them is clean and `gen --check` passes; at
distance 1 it conflicts, and `merge=union` on two adjacent deletions silently
keeps both lines in the wrong order. The merge is therefore safe to attempt, and
the operator runs `gen_pyright_test_environments.py --check` on main afterwards —
only that observes the result.

## Reviewer merges

`MEIVA_MERGE_REVIEWER=1` merges outside the lanes: conditions 1, 4 and 5 are
waived and 2 and 3 still must hold, since those are the two the machine can prove
about any pull request.

## What a NO-GO does and does not mean

A NO-GO says this lane may not merge that pull request: either one of the four
machine-checkable conditions fails, or condition 5 says the grant does not cover
its author. It is **not** a claim that the pull request is bad, and it does not
bind anyone else. A pull request from outside the grant can be perfectly good and
still be refused here by construction; the coordinator merges those on green CI
and their own review, and the gate's refusal stands beside that merge rather than
contradicting it. Reading such a merge as a bypass is the mistake this paragraph
exists to prevent.

## Run it from the repository it judges

`gh api repos/{owner}/{repo}` resolves the placeholder from the current
directory, so the process has to start inside the checkout; from anywhere else
every condition fails with `fatal: not a git repository`. An operator wrapper
should change directory to its checkout before handing over.

## Not this tool's job

The gate decides; it does not merge. The `gh` wrapper that calls it runs the real
`gh pr merge` on GO, so a GO verdict and the merge stay separate steps and a
failure in the gate cannot look like a successful merge.

## Install the shell and the shim (2026-11-08)

Two files make the gate reachable from a lane: `merge_ready.py`, which resolves the checkout and
the grant and hands over to this module, and `bin/gh`, the PATH-level shim that calls it for
`gh pr merge` and passes everything else through. Both live here since task #6411 and are tested
by `tests/test_merge_ready_shell.py`; the gate itself never depends on either.

A host installs them once:

1. Put a grant list at `~/.meiva/gates/lane-agents.txt` (one agent per line, `#` comments
   allowed). It names who may merge, so it stays outside every checkout. `MEIVA_LANE_FILE`
   overrides the location; a missing list is a refusal, not an empty grant.
2. Copy or symlink `scripts/ci/pull_requests/gate/bin/gh` onto a directory ahead of the real `gh` on
   `PATH`. The shim finds the real binary by skipping its own directory, so `MEIVA_REAL_GH` is only
   needed when `PATH` carries none -- the same variable the gate reads, since the shim passes its own
   value on.
3. Keep a checkout of this repository at `~/.meiva/source` (deployed) or `~/Meiva`
   (development): the shell resolves `merge_gate.py` from there, deployed first, and prints the
   chosen module with its short HEAD sha on stderr. `MEIVA_GATE_MODULE` overrides both.

`preflight.sh` (the operator script that runs the lane's checks) is deliberately not in this
repository yet: it is a hand-maintained list of gates that changes faster than this module, and
moving it here would give it a stability the list does not have. Its own replacement is a
separate decision.
