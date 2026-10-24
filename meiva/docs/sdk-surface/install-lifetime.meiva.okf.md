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
(`meiva/sdk_surface/install.py:Build.revert`, whose `match` names every record kind and ends in
`assert_never`). A record names the objects it touched (a wrap's parent and attribute, a member's
parent module), so reversing one needs nothing from the code that applied it. A wrap puts the
original back only while its parent still holds that wrap's own function; namespaces and members
reverse through `meiva/sdk_surface/plugins.py:uninstall`; a bound plugin config is released by the
function `bind_plugin_config` returned, which its record keeps. An installed surface is reversed
through the same ledger, rebuilt by `Build.of` from the installation's frozen snapshots.

## The sampling root retains cleanup during reload

`install()` requires the sampling owner and registers nothing itself. A bare-SDK
process builds its owner through `meiva/boot.py:process_sampling_owner`, which
registers `close` with `atexit`; a service root closes its owner through its lifespan.

A full reload resolves the owner it will retain before removing the previous surface.
`uninstall(retaining_sampling=owner)` preserves an existing exit registration only when
that exact owner was installed. The replacement keeps the same cleanup; an owner managed
by a service acquires no exit registration. An ordinary `uninstall()` removes the
installed owner's registration before reversing the surface.

If a reload retaining the owner fails while importing faces or installing the new
surface, the process root still owns that owner and its existing finite exit cleanup.
The surface may be absent after failure; that does not abandon an unfinished refresh.
This cleanup does not promise a nonzero exit code or delivery after a hard exit.

Parent: [[sdk-surface.meiva.okf.md]].
