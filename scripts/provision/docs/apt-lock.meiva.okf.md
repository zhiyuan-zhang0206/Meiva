---
type: doc
title: Waiting for the dpkg frontend lock in CI installs
description: "`scripts/provision/apt_lock.py` and its shell wrapper: why a bounded apt retry has to wait for the lock the previous attempt's apt still holds, what the exit codes mean to the composite actions that call it, and how a held lock is kept from reading as a cache or test failure."
tags:
- scripts
- ci
---

# Waiting for the dpkg frontend lock in CI installs

`apt-get` and `dpkg` hold an fcntl write lock on `/var/lib/dpkg/lock-frontend`
for their whole run. Both install actions bound each attempt with `timeout`,
which ends the `sudo` child but not the apt it started, so attempt 2 can begin
while attempt 1's apt still holds the lock. The second attempt then fails on
the lock, the loop reports exhaustion, and the `.deb` fallback runs into the
same held lock — which is how one unreachable mirror (`azure.archive.ubuntu.com`,
2026-10-22) was printed as `the cached archives cannot satisfy the system deps`
on a job whose archive cache was complete.

`scripts/provision/apt_lock.py` probes the same fcntl lock apt takes — never a
guess from process names — and waits for it with a ceiling (120s at the call
sites). Exit 0 means the lock is free, 1 that it is still held when the ceiling
expired (the holder's pid is printed from `/proc`), 2 that it could not be
inspected, which is a permission problem and not a lock. A missing lock file
counts as free and is never created here: apt creates it with the ownership the
platform expects.

`wait_apt_lock.sh` is the shell face the composite actions call. It is
**advisory**: exit 1 means the lock would not clear within the ceiling and exit 2
that it could not be inspected, and both are reported as GitHub `::warning::`
with the holder named — the install step then continues to the cached `.deb`
path rather than failing on the lock. Only that cached install failing fails the
step, and its message says whether the lock was still held or the cache itself
came up short. Both `install-playwright-deps` and `install-pg-redis` wait inside each
attempt that unpacks packages — `--download-only` does not take the lock and is
deliberately exempt.

A held lock or an unreachable archive still fails the check, and
`scripts/ci/pull_requests/diagnose.py` classifies that family as
`infrastructure: ...` with a rerun as the suggested action, so it never reads
like a red test in the report; `ci-rerun.yml` retries attempt 1 automatically.
