---
type: doc
title: Health-probe scheduler definitions and resources
description: Explicit launchd values, self-job admission and shared scheduler mechanics.
tags: [base]
---

# Health-probe scheduler

`system/cron/health_content.py` is the pure launchd definition. It consumes planned
argv, interval, output/error log path, environment home and PATH; the fixed health
label is defined here using the shared label namespace. Both the executable and
log paths are escaped as XML text. Environment encoding remains owned by
`job_values.launchd_environment`, while executable and PATH discovery stay in
`job_readings`. A pure definition does not receive Host or resolve a home.

`health_job.py` owns health registration and removal. `mechanics.py` owns the
shared home gates, executable/PATH capture wrappers, platform dispatch and actual
launchd/crontab command protocols. The package init loads none of these effects
and publishes no forwarding aliases. This separation is an ownership choice;
a single effect module would also fit the directory budget.

Registration still checks enabled, default-home ownership and production-checkout
refusal before platform dispatch. On macOS it observes the destination Path.home
before checking its own launchd label and process ancestry. An own-job call returns
without mkdir, definition capture, writes or scheduler commands: bootout would kill
its own tree. An external call creates the directory, resolves the executable,
reads the log home, reads the environment home and composes PATH, in that order.
PATH still separately observes the executable and user home. These observations
are not merged into one atomic JobRoots snapshot.

External registration always writes and reloads, even for equal content: write,
bootout, bootstrap. Bootout status remains ignored here; bootstrap failure returns1.
Removal still bootouts, probes with print, unlinks the plist and reports an
unproven retirement with rc1 and a launchd_retirement error. The existing outer
unregister dispatcher ignores remover return codes; this contribution changes
neither that interface nor any process-custody proof.

Linux retains its capability gate before executable/home reads, the original
minutes clamp and command text, marker substring ownership, and lock-enclosed
read/filter/write. Only benign no-crontab is an empty table for registration;
other read failures cannot clobber it. Existing error messages/return codes and
unrelated/watchdog entries remain unchanged. Linux serialization and its existing
shell-quoting limitations are outside the macOS value extraction.

Tests parse real XML and write private files in fresh children. Scripted or
refusing CommandRunner resources are intentional: private HOME does not isolate
the operator's real scheduler. They observe actual command order and equal-content
reload rather than installing launchd/crontab jobs. Ancestry's existing test
substitution remains explicitly limited evidence, not real launchd integration.
