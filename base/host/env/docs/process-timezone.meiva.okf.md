---
type: doc
title: Process timezone delivery
description: Explicit startup timezone application and the read-only configuration attachment boundary.
tags:
- configuration
- environment
---

# Process timezone delivery

`base/host/env/boot_actions.py:apply_process_timezone` owns the process clock
operation. Its caller supplies the resolved authoritative timezone, or `None`
when no authority exists. The operation does not read Settings, `MEIVA_TIMEZONE`
or a timezone field default. A valid IANA name sets `TZ` for child processes and
calls `time.tzset` on platforms that support it. Missing authority leaves the
host clock unchanged. An invalid name that bypassed configuration validation
also leaves the clock unchanged, preserving the existing startup behavior;
raw configuration input still fails at its validation boundary.

`ConfigBoot.prepare` applies this operation after its startup environment and
configuration fields have been prepared. `ConfigBoot.read_process_environment`
captures an existing delivery without applying it, including during later eager
model construction. SDK attachment therefore never changes the process TZ.
The legacy `base.config.apply_cluster_timezone` facade resolves its existing
cluster authority and calls the same operation; it does not own a second
implementation.

The operation is defined in `base/host/env/boot_actions.py`, the module of the
program-level operations that change the process itself (`apply_process_timezone`,
`skip_config_fetch`, `enter_scratch_home`) and the root of that kind, below configuration
composition. ConfigBoot imports that definition directly, so a standalone configuration
owner does not import a private operation from the root Settings facade. `dotenv_boot`
works on the mapping it is given and imports none of them. This change does not retire the
legacy Settings facade or its full-build graph.

## Backup filename test premise

`base/tests/config/test_dotenv_file.py` places the host and decoy cluster zones
26 hours apart, so their calendar dates differ at every instant. It samples
before and after snapshot creation to tolerate crossing the host's midnight.
Restoring the `TZ` environment variable also requires `time.tzset()` to restore
the C library's cached timezone for later tests.
