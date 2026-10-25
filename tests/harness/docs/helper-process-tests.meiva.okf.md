---
type: doc
title: Helper process contract tests
description: Unix-socket spawn servers, durable ownership and real ancestor-chain probes.
tags: [tests, process, helper]
---

# Helper process contract tests

### Helper spawn server

The tests drive `PermissionsHelperClient` over a real temporary Unix domain socket
server speaking the helper's JSON-line protocol. Spawn requests and refusal
responses exercise the production client wire contract directly without
subclassing `PermissionsHelperClient`.

### Helper record and identity

Durable session records own spawn idempotency. Final graceful delivery accepts the same birth-time tolerance as resolution and rejects a birth outside that tolerance.

A helper's volatile nursery table cannot replace the persistent record. The
whole-second drift case preserves the macOS boot-time correction rationale:
resolution and final signal delivery must agree about the accepted birth.

### Helper ancestor chain

The former direct-parent assumption mistook `unit -> root -> helper` for an
orphan and terminated the unit during boot. The tests preserve that geometry.

### Helper probe executable

The `S603` exception belongs only to this fixed test executable. It does not
admit operator-supplied executable text, and the call retains its 60-second bound.
