---
type: doc
title: Readiness roster ownership
description: Service probes and diagnostic helpers are documented from their actual registrations.
tags:
- services
---

# Readiness roster ownership

`ServiceSpec.identity_probe` is mandatory for selected root units. The roster
uses each declared application protocol, including plugin services. `healthcheck_module` identifies protocol code for documentation;
placing a module in this directory does not schedule it.

The documentation linter compares module files with the table and with the
union of service metadata and imports in `roster/composition.py` and the root
diagnostic adapters. It parses those sources without constructing a live
host-dependent diagnostic roster or probing services. Static imports identify
the modules those owners depend on; they are not proof that a probe was
scheduled or that every runtime registration was discovered.

PgBouncer diagnostics traverse its admin console and required listeners rather
than an end-to-end Postgres query. Redis diagnostics traverse runtime ACL PING.
Neither performs native custody inspection, starts a process or repairs credentials. Their resources must remain available when the
application tree is stopped for external maintenance.
