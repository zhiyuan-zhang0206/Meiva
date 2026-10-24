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

## The sampling owner is named by the root

`install()` takes the sampling owner as a required argument; nothing below a composition root builds
one. A root that owns a whole process with no service lifespan (the bare-SDK process, a CLI verb)
builds it with `process_sampling_owner()` and registers its `close` with `atexit` itself; a service
root (the agent host) closes the owner it built in its own shutdown. `uninstall()` unregisters the
installation's owner `close` from `atexit` before it drops the slot, so a removed surface leaves
nothing behind that could re-raise a refresh failure the surface no longer owns, while an
installation left in place still reports that failure at interpreter exit. This is finite cleanup,
not a promise of a nonzero exit code or of delivery after a hard exit.

`load_extensions(sampling=...)` names the owner of a first load; a reload that names none keeps the
previous installation's owner and refuses when there is none.

The owner reads the live process environment (`process_environment()`), so a gateway URL, machine
token or flag that appears after install is seen at the next refresh. The snapshot it serves until
its first refresh is the policy that environment already carries, and the first refresh is due one
refresh interval after the owner is built.

Parent: [[sdk-surface.meiva.okf.md]].
