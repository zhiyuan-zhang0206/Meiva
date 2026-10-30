---
type: doc
title: Host primitives
description: Operating-system integration, environment ownership and local host utilities.
tags: [base]
---

# Host primitives

`base/host/` owns operating-system integration, environment ownership and local host utilities.
Its component nodes describe the current contracts and implementation.

`net` groups config-free URL and host predicates, IPv4 HTTPX dials and retry
helpers. HTTPX uses its installed import behavior and public entry points.

## Documented components

- [[ops/docs/env-audit.meiva.okf.md]] — Base — .env write audit & integrity guard.
- [[base/host/env/docs/registry.meiva.okf.md]] — Environment declarations and boot/child projections.
- [[base/host/docs/runtime_interpreter.meiva.okf.md]] — Loaded-runtime interpreter binding.
- [[base/host/docs/resilience.meiva.okf.md]] — Retry primitives, classification and the wait seam.
- [[base/host/docs/brew_pin.meiva.okf.md]] — Operator-approved tool pins: brew formula manifest, uv release digests, apt-series pins, brew probes.

`base/host/system/hostport.py` is the port to this machine. A component that starts a process,
looks up a program or branches on the platform takes a required `host: Host` with five fields:
`run` (`proc.run_bounded`, whose timeout bounds the whole process tree), `which`
(`shutil.which`), `platform` (`base.native_process.os_platform.Platform`: `system` as `linux` or
`darwin`, `machine`, `executable`; it is defined there once because `native_process` sits below `base.host`), `uid` and `environ` (the process environment, read-only). The composition root builds it with `Host.system(environ=...)`; a test builds the
`Host` it wants with `base.host.system.tests.fake_commands.FakeCommands` as `run`, which records each
argv, answers from a script routed by argv prefix and refuses an unscripted command. A test that
needs a real process uses `Host.system(environ={})`. The five fields are the whole port; a new capability
is a reviewed addition, not a field that appears on its own.

`base/host/atomic_io.py` publishes a file by writing a sibling temporary file, syncing it,
renaming it over the target and, when asked, syncing the directory. The three calls whose failure
a test must cause are the fields of `Fs` (`replace`, `fsync`, `fchmod`), and an `AtomicWriter`
runs every write through one. `write_text_atomic`, `write_bytes_atomic` and `fsync_parent` are the
writer over `SYSTEM_FS`; a caller with no policy of its own calls them, while a caller whose own
policy warns, suppresses or never syncs takes a required `AtomicWriter` ([python
conventions](../../../docs/conventions/python-conventions.md)). The module's own tests build
`AtomicWriter(Fs(...))` with the failure they need (a failed replace keeps the old content, a
failed file sync never commits, a failed directory sync leaves the new content committed); other
components' tests use real files and patch none of these calls. A converted caller proves its own
policy with the one `AtomicWriter` its test builds.

`base/host/net/resilience.py` owns the shared retry primitives: immutable `Policy`
parameters, the `retry` / `aretry` executors and the wait seam a caller passes its
own clock through. [[base/host/docs/resilience.meiva.okf.md]] is the current node.

`base/host/net/ports.py` is the port for an HTTP request whose answer a test controls.
`UrlOpener` has `urllib.request.urlopen`'s shape (a request or a URL and a timeout in, a readable
context manager out whose surface is `read` and `read1`: a caller that owns a wall-clock budget
reads with `read1`, one socket read per call, instead of an aggregate `read` that waits for the
requested size); a component that makes the request takes it as a required argument and a
composition root passes `SYSTEM_URL_OPENER`. `base.host.net.tests.fake_url_opener.FakeUrlOpener`
records each request and answers from a script routed by URL prefix, raises `HTTPError` for a
status outside 2xx as `urlopen` does, and refuses a URL nobody scripted. A test that wants the
real client starts a listener on port 0; none patches `urlopen` or `socket`.

The private-storage live Unix-socket test uses a short `/tmp` root so macOS's
AF_UNIX path limit does not fail at bind before the test reaches socket skipping
and ordinary-file mode repair.

Drive convergence receives the existing `Host` and an explicit user home when
enumerating candidate folders. The lifecycle CLI captures those candidates once,
passes them to the real write/read/delete probe, and reuses the same paths in its
warning. A Drive mount is never accepted from directory existence alone.

Every OS job (health probe, boot autostart, logs, packages refresh, PR flow and WAL-G),
registering or retiring, receives `JobRoots` from its command's existing home, checkout and
user session values. Platform dispatch remains in the existing `Host` resource shell. Their `*_content.py` owners render commands,
launchd XML and cron entries without `Host` or ambient path reads. The registrar
first applies its existing switch/home/policy gates, then captures the checkout
binary and, for launchd, the PATH. PR-flow checks production, `gh` and the token
file before capturing executable locations; token contents never enter a job value.
`RefreshSchedule` belongs to `packages_content.py`, including its existing validation.

`job_readings.py` is the one executable/PATH capture policy, also used by the
health composition in `cron/health_job.py` and boot registration in
`system/boot_autostart/registration.py`. `job_values.py` owns the shared
label namespace, plist destination and environment encoding. Shell assignments
quote the home, and XML string values escape XML characters after shell quoting.
The four complete maintenance definitions support spaces and XML metacharacters;
boot autostart definitions now also escape their output/error log paths. This does
not claim to repair untouched health-probe fields.

Boot autostart definition inputs and capture order are described in
[[base/host/docs/boot-autostart.meiva.okf.md]].

Registration still protects the scheduler's default-home namespace at its write
boundary. Removal retains its ambient-home capture and the same protection.
The existing crontab lock encloses the read, decision and write; a failed read
cannot rewrite the table. Command-protocol tests retain scheduler error/return-code
coverage. Separate tests execute rendered commands in disposable files with bounded
real shells and parse real XML; they do not register in the user's scheduler.

Health-job value capture and custody are described in
[[base/host/docs/health-probe-job.meiva.okf.md]].
