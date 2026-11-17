# Cached entry waits for its accepted authority read

A retained timeline window could make a new selection ready before its current
authority request completed. Home could then restore a bookmark against cached
geometry, and an unchanged response could go unobserved through query structural
sharing.

Each selection now owns its request receipt and accepted composed-read revision.
The store reports whether it applied the snapshot, so a pre-compact response
dropped during reset cannot release readiness. Home gates restoration separately
from loading presentation: a failed read exposes retry and settles the ring
while preserving the pending bookmark. Late A1 completion cannot qualify B or
A2; background reads after readiness keep the existing loading presentation.

The direct Home, timeline hook, reconcile and store consumers cover unchanged,
changed, empty, error/retry and rapid-selection paths. Direct store assertions
distinguish applied reads from compact-reset drops. The four additional Home
scenario writes move its test-double ledger from 107 to 111; the current checker
table and exact-accounting test follow the source scan, preserving prior counts.

This change establishes request completion and actual store application. Existing
snapshot merge may retain committed rows absent from a response; an empty ready
response establishes no complete cache eviction or durable session identity.
Refresh persistence and historical source-window seeking remain separately owned.
