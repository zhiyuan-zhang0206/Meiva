---
type: doc
title: Configuration at the composition root
description: The root reads the view once and hands each component a frozen config value; the view never leaves the root module.
tags: [configuration]
---

# Configuration at the composition root

`ConfigBoot` is one composition owner's configuration. A process entry (the composition root)
calls `boot()`, then reads `view` and builds the frozen `XxxConfig` value each component needs.
`view`, and any closure over it (`lambda: view.x.y`, a `path_reader`, a `runtime_view(request)`),
stays in the root module: a component takes the value, never the view.

This replaces the earlier guidance to pass the view or narrow reader closures to consumers. A
closure hides which settings a component reads (the locality tools see `view.x.y` as ordinary
attribute access), makes a test patch the reader instead of naming the setting, and turns "when is
this read" into a property of the closure's call site.

## Shape of a component config

- A frozen dataclass named for its component, holding exactly the fields the component uses.
- Built by the root, either field by field or by `XxxConfig.from_view(view)`. `from_view` is
  called only from a root; a component never calls it.
- Validated at construction; an invalid value fails the root's boot, not a later request.

## A setting that must change while the process runs

The component is handed the `ConfigBoot` (a real resource, form 3) or an authority built from it,
and builds a fresh `XxxConfig` at its unit of work: a request, a turn, a refresh tick. The unit of
work, not a stored closure, decides when the setting is read.

## What stays outside this rule

Tests build the `XxxConfig` value directly. `ConfigBoot.set_field` is for a test that needs the
real owner (config round trips, the admin panel), not for steering a component.

## Configuration behind an earlier gate

Read conditional policy only after the facts that make it necessary. OS-job convergence checks
its master switch and home ownership before resolving a refresh schedule; a disabled refresh
channel has no tick. The `os_jobs_enabled` and `refresh_enabled` lite-manifest rows serve these
gates without constructing Settings and validating the unused tick. An active channel still upgrades and
validates the configured interval. The generated rows take their aliases and defaults from the config
owner; it does not introduce a second parser. Malformed lite flags still fail at the existing
boot boundary, including when a later gate would skip the job.
