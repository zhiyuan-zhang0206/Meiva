---
type: doc
title: SDK installation lifetime — retained clock, event clients and atexit owner
description: What an installed SDK surface keeps for the process lifetime, and what uninstalling it removes.
tags: [sdk, lifecycle]
---

# SDK installation lifetime

An SDK installation retains the Clock value its root supplied (`Installation.clock`).
Timestamped operations obtain their clock from a bound context, or from the
installed clock when unbound; a partial context missing a
clock fails explicitly rather than borrowing the installation.
`meiva.sdk_surface.process_context.loaded_code_image()` returns the immutable
first-load code fact and does not reread a moved checkout on reload or late
attachment.

SDK recorders capture the installation's retained clients (`Installation.clients`,
whose event pipeline builds lazily), or the admitted caller's ClientSet with
identity, tally and durable capture. Changing context mid-call cannot redirect
that event. Sampling precedes pipeline construction; durable capture precedes enqueue. An unbound Python caller uses
the installation's retained clients as a system caller without creating an
MeivaContext, and reload preserves those clients.

## The reversal ledger is data

`Installation.undo` is a tuple of records, not callables: `NamespaceAdded`,
`MemberAdded`, `WrapAdded`, `ProviderAdded` and `ConfigBound`
(`Reversal` in `meiva/sdk_surface/installation.py`). Each primitive that changes the process returns the
record of what it did; `uninstall()` and the rollback of a plugin whose declaration failed
halfway read the ledger newest-first and reverse each record
(`meiva/sdk_surface/install.py:_revert`). A record names the objects it touched (a wrap's parent
and attribute, a member's parent module), so reversing one needs nothing from the code that
applied it. A wrap puts the original back only while its parent still holds that wrap's own
function; namespaces and members reverse through `meiva/sdk_surface/plugins.py:uninstall`.

## The atexit owner follows the installation

`install()` registers the sampling owner's `close` with `atexit` when the caller
supplied no sampling owner — the bare-SDK process, which has no service lifespan
to schedule that cleanup. The registration belongs to that installation rather
than to the process at large: `uninstall()` unregisters the same `close` before
it drops the slot, so a removed surface leaves nothing behind that could
re-raise a refresh failure the surface no longer owns, while an installation
left in place still reports that failure at interpreter exit. This is finite
cleanup, not a promise of a nonzero exit code or of delivery after a hard exit.

The process-lifetime sampling close is registered with, and removed by, the installation that owns it: uninstalling the SDK surface unregisters that owner's close, so a removed surface cannot re-raise its refresh failure at interpreter exit, while an installation left in place (the bare-SDK process) still reports one.

Parent: [[sdk-surface.meiva.okf.md]].
