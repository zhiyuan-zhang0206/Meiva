---
type: doc
title: "Required whole-file backend shards"
description: "Identity-bound shared full plans, concurrent runner preparation and fail-closed worker collection checks."
tags:
- infrastructure
- testing
---

# Required whole-file backend shards

Every FULL backend population uses one complete eligible collection. The
`backend-plan` producer and eight shard runners share `classify` as their only
routing dependency, so native environment setup overlaps planning. Their FULL
predicate is identical, including unknown impact, shadow mode and selector
failure. SELECTED populations retain local selected-file planning and do not
start the full producer. Planning errors, configuration drift and collection or
worker validation errors fail directly without fallback or retry; ordinary test
failures retain the existing retry and canary policy.

Classification resolves the exact duration-cache key once and records its actual
execution attempt. The producer publishes the plan with the exact duration bytes
it used. A zero-byte bundle member represents a missing input; an empty JSON map
remains a present input. Consumers never resolve another timing cache.

After environment setup, each shard waits within a bounded deadline for this
run's successful producer and downloads its exact artifact ID. All paginated
job records are checked against the source head and run. The classifier's saved
attempt is the minimum eligible producer generation: a full rerun cannot consume
a previous plan while its new producer is registering. A failed-only rerun may
reuse a successful producer that did not execute again. A newer queued or running
producer blocks older success; current-generation failure fails directly. Prior
failure can wait only within the same bounded registration budget. Carried-forward
job records retain their successful generation-marker step. The producer owns
that explicit generation, validates it against its actual environment, and pairs
it with a bundle receipt. A new job ID or run-attempt view cannot advance the
marker. Missing, duplicate, failed or malformed markers are rejected; no timestamp
fingerprint guesses an execution identity. The consumer checks the bundle receipt
against the marker before restoring input.
Artifacts are named by run and actual producer attempt and must be unique,
unexpired and tied to the same source. API errors, malformed metadata, incomplete
pagination and deadline expiry fail closed.

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
the required producer and artifact on FULL routes even when shard jobs are skipped.
Environment preparation overlaps the producer instead of following it. Runner
queue delays can still change the critical path; lower runner usage does not
alone prove a wall-clock gain. Compare actual setup, planning, handoff and test
timings before claiming one.

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
