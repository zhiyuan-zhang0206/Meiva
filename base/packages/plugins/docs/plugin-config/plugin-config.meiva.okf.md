---
type: doc
title: Plugin config images and agent views
description: Explicit ownership, boot overlays and validation of plugin configuration.
tags: [base, plugins]
---

# Plugin config images and agent views

Ownership follows the
[plugin config image decision](../../../../../docs/decisions/extensions/plugins/2026-10-09-plugin-config-images-have-an-owner.md).

The config face declares one frozen Pydantic model. Its full authority image
lives at `$MEIVA_HOME/configs/<plugin>/config.json`. Reading a missing image can
use declared defaults; binding creates that initial image. Existing malformed
data and schema drift raise their explicit errors, without repair or fallback.
`meiva plugins update` owns schema reconciliation.

The plugin loader selects one home and supplies a required `PluginConfigImages`
owner with its load result. The SDK installer receives that owner; it neither
discovers paths nor constructs a disk adapter. Image owners are created lazily
for each declaration, so a plugin keeps its own validation and rollback boundary.
The installation retains this owner for restoration and value replacements. A
bare skill-provider mount carries an explicit unbound image capability; it does
not resolve a home or gain config access until an actual load supplies an owner.
The disk implementation uses the existing locked whole-image CAS. Only the first
missing read may use defaults; after creation or a competing writer, the final
read is strict. A disappearing or invalid winner aborts binding without publishing
a config. Unknown read and create errors propagate unchanged.

The SDK installer binds instances into its local build and publishes them on
`Installation.configs`. The declared class is the instance's type; there is no
second registry to synchronize. Rollback removes local bindings and a failed
installation publishes nothing. The SDK module reads its current installation;
services and agent views receive the resolved image explicitly.

The exec child applies core overlay fields before loading the SDK and plugin
overlay fields after installation. The latter builds a replacement config
image and the installer publishes it. Frozen instances and the caller's input
mapping are not mutated. This does not introduce in-process plugin reload.

The agent host holds its boot image and supplies it when resolving
`AgentSlices`. `PluginConfigView` layers only that agent's pins over the image,
memoizing frozen instances within that view. External attachments construct
the same view from their process's installation. Separate roots and agents
do not share a registration table or overlay state.

Plugin config has no `birth_config`: without an explicit agent override, a
new process reads the current authority image. Core frozen/live field rules,
fresh-file config-service reads, outbox ticks, and bootstrap delivery retain
their own existing update boundaries. Provider credentials remain in the
declared secret-delivery channel, outside plugin config images.

Panel metadata evaluates default factories and gives required fields a null default;
Pydantic's undefined sentinel never reaches JSON.

Gateway and ops need no SDK installation to validate an overlay. They read
enabled config declarations; an SDK caller additionally supplies its bound
image so validation retains its existing base values. Unknown keys, ambiguous
owners, non-overridable fields and invalid values are rejected before writes.

`config_registration.plugin_bootstrap_config` builds the gateway cluster-policy packet
from discovered declarations and fresh authority disk images. It includes only
declared `cluster-pinned` and `cluster-default` fields, rejects sensitive fields,
and retains strict schema admission. The framework config reader receives that
packet explicitly and does not discover plugins or own their authority images.

`PluginEnableStore` owns the selected enable-file path. Canonical process
functions construct it for the normal home; isolated consumers supply their own
path. Directory scanning receives its builtin and external roots explicitly,
while the process entry resolves the normal roots once. Missing enable files
retain defaults, malformed JSON and non-object images raise, and local updates retain deterministic
ordering and scope to the plugins present on that machine.

The CLI and normal start convergence own schema reconciliation; there is no
gateway endpoint for this operation. Each class comes from the pure
`default_config.py` face, without importing `plugin.py`. Each plugin reports its
result independently, so one invalid image does not prevent unrelated merges.

## Schema reconciliation

`meiva plugins update` merges the disk image to the declared schema: new fields take their class defaults, removed fields are dropped so the strict equality check in the disk reader keeps passing — a stale field would make every spawn raise SchemaDriftError — and an incompatible type change raises InvalidConfigData for manual migration. An aligned image is a no-op; a missing image receives the defaults.

## Overlay admission

SDK-side overlay validation splices the overlay into a FRESH declaring sub-model built from env + defaults — never the validating process's own settings, whose profile may hold only some domains (reading the old settings was the #4423 boundary 500) — then runs the framework range validators; plugin fields validate against the declared class. Success validates only: settings and the supplied image are not modified. A flat key must uniquely match a framework field or a plugin Config field, and only per_agent fields are overridable; violations raise InvalidConfigOverlay naming the key and reason.

## Framework range validators

The complete per-agent overlay surface with a value universe is range-checked: model names against the catalog (an unregistered id would crash the next boot at model build — #1704), durations / timeouts strictly positive and finite (a NaN never fires, an Inf defeats a cap), probabilities in [0, 1], fractions in (0, 1], counts non-negative; None passes (the unset sentinel) and non-numerics fall through to Pydantic. Plugin config fields are deliberately not range-checked — their schemas own their semantics.

## Service birth snapshot

A launched service validates its birth snapshot: a present packet must name this plugin and contain its complete declared image; malformed packets never fall back to disk or defaults, and an absent carrier is distinct from an invalid one.

View contracts: [[config-view.meiva.okf.md]]

Gateway overlay validation tests boot the actual gateway profile in fresh
processes, where the agent domain is absent. Framework-scope overlay tests
attach independent configuration read owners to the delivered environment;
only the explicitly supplied setter changes, and plugin configuration remains
unchanged. These attachments do not rerun process environment delivery.

The batch image updater receives a required discovered plugin-directory map and
selected home. CLI roots supply actual discovery and their unit home; it does
not discover a second set of plugins or infer a storage home during iteration.
Each entry constructs the shared schema-owned image and calls the same required
merge operation. Tests supply actual temporary directories and images, retaining
schema shrink, exact values and per-plugin error isolation without changing
process-wide paths.

The service carrier is delivered by the launcher before process imports. Its
snapshot contract tests run a fresh interpreter with an explicit, minimal
environment: the captured image remains stable after authority-file edits,
absence returns no snapshot, and malformed or incomplete packets still raise.
Flag admission is exercised through an actual gateway profile and a valid
agent-domain declaration, rather than pretending a constructed daemon domain
is unavailable. Neither path changes the process-wide configuration owner.
