# Explicit refresh deltas and lazy harness exports

The fixed-field debt pass found two finite protocols hidden behind dynamic
attribute names: refresh stages a known set of registry writes, and the harness
facade serves eight known exports. Both can name their fields directly.

Refresh retains its delayed, locked merge into the freshly loaded registry.
A staged delta carries typed fields instead of arbitrary attribute names. The
apply timestamp owns the three content bookkeeping writes, including clearing
a missing content hash, so an unapplied result leaves those fields alone. A
complete replacement built from the earlier snapshot was rejected because it
could overwrite another writer's changes before the final lock is taken.

The harness facade keeps resolving exports on demand. Eager imports were
rejected because importing the SDK must remain light and must not attach an
identity. Each export now imports its named object explicitly. This removes
three dynamic attribute reads while retaining the separate lazy-facade debt.

These changes repay five recorded occurrences without changing rules,
allowances, roots, public names or feature support. Other entries whose reason
mentions fixed fields need separate review when the implementation actually
walks extensible schemas or resolves identity-dependent values on each read.
