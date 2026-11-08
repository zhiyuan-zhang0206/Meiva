---
type: doc
title: "schedules/jobs/watch/ — Inherited Reds at the e2e Gate"
description: "Why `e2e gate` can pass on a failed e2e suite, the declared set it excuses against, the four ways it refuses instead, and how one entry is added and retired."
tags:
  - ci
  - testing
---

# schedules/jobs/watch/ — Inherited Reds at the e2e Gate

## What it is

`e2e gate` is a required check. Before 2026-11-08 it failed whenever the e2e suite failed, so one
deterministic red on `main` froze every pull request that runs e2e — the cost of a single red was
paid by everyone who did not cause it (task #6480). The gate now asks a narrower question: are all of
this run's failing test identities reds `main` already has?

`schedules/jobs/watch/inherited_reds.py` answers it. A pass is still an event, not an absence: the
check summary prints the nightly run it trusted (id, attempt, sha, completion time and age), whether
the pull request's base stands on that run's head, every red it waved through with its card and
expiry, and every red it did not.

## The decided set

`schedules/jobs/watch/inherited-reds.toml` lists them, and every entry carries the card that owns the
fix and the date the excuse dies:

```toml
[[inherited_red]]
nodeid = "tests/e2e/state/test_timeline_virtualization.py::test_parked_deep_history_mounts_a_bounded_window[agent_chat]"
card = "#6475"
expires = 2026-11-15
```

- The set is never written by a run. Adding or extending an entry is a pull request, so the person
  who excuses a red signs for it.
- An expired entry excuses nothing. The red comes back on its own, and the fix is the card's, not
  this file's.
- An entry excuses a failure only while that identity also failed in the nightly this gate read. A
  declaration is a signature on top of the evidence, never a replacement for it.
- `nodeid` is the watch's own fingerprint, `<file>::<test name>`, built by
  `schedules/plans/watch/junit.py` from the JUnit `file`/`classname` attribute. One test keeps one
  identity whichever job ran it: `e2e shard (3/4)` on this workflow and `e2e full matrix (3/4)` on
  the release gate are the same red, and a job-name set would count it twice.

## Why the gate lives with the watch, and not in `scripts/ci/`

The identity vocabulary is the watch's (`schedules/plans/watch/junit.py`, task #6487), and the gate
decides the same question the watch reports on: which reds `main` already has. `scripts` cannot
import `schedules` — `schedules` already imports `scripts`, so that edge closes a cycle — so a gate
in `scripts/ci/` would need the vocabulary moved down, which that directory's 20-entry budget no
longer has room for. The decision therefore lives where the vocabulary and the fact do, beside the
watch job that reads the same runs (`ci_metrics.py`, `api_read.py`), and `ci.yml` calls it as the
`e2e gate`: `python -m schedules.jobs.watch.inherited_reds`.

The tests are `schedules/tests/watch/test_inherited_reds.py`, beside the watch's other tests.

## The four refusals

A pass needs an authoritative set, and every way of not having one fails the gate. The alternative —
no set, therefore no inherited reds, therefore nothing to see — is how this mechanism would become a
new kind of false green.

1. **No nightly.** No completed scheduled run of `ci.yml` on `main`, or its age is over 30 hours
   (one daily cadence plus the measured 4.9–5.5 hour phase drift). The watch itself is broken then,
   and its set describes nothing.
2. **No base.** A failed suite with no `pull_request.base.sha` (a dispatch or the nightly itself) has
   nothing to be inherited from.
3. **Base off the nightly.** `compare(nightly_head...base)` must answer `ahead` or `identical`: this
   pull request's base contains the tree the nightly tested. `behind` and `diverged` mean the base
   lacks commits the nightly saw, and an excuse for a state this base does not contain is invalid
   rather than merely old. The direction matters and only this one can fire: the pull request that
   declares an inherited red is merged *after* the nightly, so by the time any later pull request
   asks, `main` is already past the nightly head; asking the other way round would make the
   mechanism unreachable by construction (405, 2026-11-08).
4. **No evidence.** The run's own attempt must yield the failing identities, and the artifacts must
   come from that attempt: an artifact carries no attempt field, so it is bound by *its job's name*
   (the artifact name derived from the failing job) and *its job's time window*. Fetching by name
   alone returns the newest artifact of that name — a rerun's green one — and turns a red set into an
   empty set that looks correct.

`declaration` itself is read the same way: a file that does not parse, an entry without a card or a
date, one identity declared twice, or a TOML datetime where a date belongs refuses the gate with a
message instead of excusing nothing.

## What the gate prints

```
e2e gate: PASS - every failure is inherited from run 100000001
nightly: run 100000001 attempt 2 sha 0a1b2c3d4e5f completed 2026-11-08T02:31:00+00:00 (5.2h ago)
base is an ancestor of the nightly head: yes
inherited red 1:
  - tests/e2e/state/test_timeline_virtualization.py::test_parked_deep_history_mounts_a_bounded_window[agent_chat] (card #6475, expires 2026-11-15)
```

A red this pull request added prints under `new red N:` and fails the check. Nothing was loosened to
produce a pass: no assertion, timeout or skip changed, and the suite still runs.

## Reading the code

`judge` is the decision and takes data only; `load_declaration`, `failures_of_attempt`,
`completed_nightly`, `base_is_ancestor` are the four reads that feed it, each failing closed; `main`
is the entry `ci.yml` runs. `Api` is what the decision needs from GitHub, so the tests hand in a
fake and never patch the module.
