---
type: doc
title: "Required whole-file backend shards"
description: "Identity-bound shared queue plans, runner-local PR plans and fail-closed worker collection checks."
tags:
- infrastructure
- testing
---

# Required whole-file backend shards

Each ordinary pull-request runner creates a fresh complete eligible plan from its
current checkout before starting four workers. Planning uses the same native
environment, filters, configuration overrides and duration input as execution.
Each worker collects only its group's files and checks the exact planned nodes
and declared fixture closure before test bodies. Planning errors, configuration
drift and collection/worker validation errors fail directly without fallback or
retry; ordinary test failures retain the existing retry and canary policy.
Duration measurement retries restore the committed input before executing the
same plan, and attempt reports remain distinct.

Trunk queue branches and manual dispatches are known FULL before classification.
Their `backend-plan` job starts alongside `classify`, performs one complete
collection and publishes a plan for all eight runners. It owns that population's
duration-cache key; every consumer restores exactly that key, including its empty
no-cache value. Workers wait on both jobs and download the exact published artifact
ID. Ordinary PRs, including unknown-impact FULL decisions, retain local planning;
selected subsets do not start a speculative complete collection.

Shared plans require schema v2 and `--file-shard-shared` on producer and consumers.
Before restricting collection, the plugin compares the exact Git tree, clean
tracked index and worktree, lock bytes, installed distribution names/versions,
Python implementation/version/platform, loaded module plugins and original
collection roots. The existing configuration and duration checks still apply;
configuration includes ignore, ignore-glob and deselect inputs. Python hash-seed,
plugin-autoload, locale/timezone and runner-image controls must match as well.
Application environment isolation remains owned by the same tree's pytest
bootstrap: worker-specific homes, leased ports and event channels are resources,
not shared plan inputs. Untracked diagnostics and generated test artifacts do
not count as tracked source drift.

Every real worker still collects its group's files and compares every node and
fixture closure before executing bodies. Invalid/missing identity, tracked drift,
missing artifacts and producer/worker failures fail closed; neither a local replan
nor a node-split fallback can hide them. The backend aggregator explicitly checks
the required producer and artifact even when downstream jobs are skipped.
The single additional job can add queue delay; lower runner usage does not alone
prove a shorter workflow critical path. Compare actual plan/setup/test timings
before claiming a wall-clock gain.

Plans remain disposable evidence for one run, not a discovery cache. Planning
diagnostics and per-worker reports accompany the existing native reports; their
publication failures do not override successful native tests.
The shared plan is a required consumed input: upload retries once on transport
failure, and only a successful upload supplies the artifact ID. An absent ID
fails the producer/consumers and backend aggregator; transport retry never replans.
Selection routing, serial/static
lanes, native JUnit verdicts and the combined coverage gate retain their owners.

The enforced selected subset plans the same way over only its selected files
(`plan_file_shards --files-from`), once per shard job, with as many groups as the selector
chose for its estimated time (see [[.github/test-selection.meiva.okf.md]]). Its plan,
check and execute options match the full shards', which the plan digest enforces.

Planning and migration evidence: [[file-shard-shadow.meiva.okf.md]].
