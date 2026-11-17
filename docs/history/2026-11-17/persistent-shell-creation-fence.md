# Fence persistent shell creation against terminal transitions

Review task #7096 identified a creation race in the initial Phase 1B change:
the per-agent `session_index` allocation committed before the PTY service
created the session and before its TTL row was inserted. A terminal transition
could capture a cutoff and finish its host cleanup during that gap. If the agent
then resurrected, the delayed creator could publish a pre-terminal PTY and TTL
row which no terminal-owner reaper would select.

The creation path now holds the `agents_meta` row write lock from counter
allocation through PTY creation and TTL insertion. Terminal transitions use the
same row lock to capture their cutoff. They therefore either wait for the new
session to be committed and include it in cleanup, or commit first and make the
new allocation refuse. A failed PTY start or TTL insert still commits the
allocated counter value after disposing of the PTY, preserving the invariant
that an old numeric shell handle never identifies a later session.

The regression test blocks a creator after it has allocated an id, proves that
a terminal row lock cannot pass it, then exercises terminal transition,
resurrection, and host cleanup. It verifies both the original session and its
TTL row are removed, and the next session receives the later id.

Verified merge base for this branch was `ae93a9fb7d462926b080b79b194ed8acbe4521fb`.
This change does not alter Mermaid assets.

Forward: [lost-ACK terminal fencing](persistent-shell-lost-ack-fence.md)
records the later finding that the database fence alone cannot cover a PTY
request whose client timed out after sending it to the service.
