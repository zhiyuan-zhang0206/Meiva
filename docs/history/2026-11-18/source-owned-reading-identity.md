# Source-owned reading identity before refresh persistence

The refresh work chose reader-time projection for durable reading identity.
Current and retained message bodies, their source domains and stable identities
come from one consistent source read. This keeps optional UI positioning out of
the live execution path: generating a message does not acquire a new database
read or failure dependency solely to mint a bookmark.

Display indexes were rejected as durable identity because compaction and session
replacement can reuse them. Native epochs select a source domain; matching a
message across domains requires a stable message ID or an exact inbound/block
identity. Record readers bind complete rows to the current registry, session
and harness. Source-issued locators and directional cursors carry that verified
membership. A separate `read_near` parser was rejected so window reads share the
existing record parser and projector instead of creating another interpretation
of the same bytes.

A bounded read that exhausts its budget does not establish that a message was
deleted. Missing identity, unavailable retained data and insufficient budget
remain distinct refusals. Callers must preserve the previous bookmark through
those refusals; a successful empty page establishes a directional endpoint only
for the source view that produced it.

This stage supplied the source boundary. Refresh persistence, an independent
parked history window, exact viewport restoration and confirmation of newly
visible live messages remained the separately owned Home stage. Its acceptance
required actual page reload and browser session storage as well as source proof.
The source and Home work carried no rollout authorization: the concurrently
approved deployment target stayed fixed at
`4d133cff66cb984b9697582811d517d7f90a1b97` until a separate deployment decision.
