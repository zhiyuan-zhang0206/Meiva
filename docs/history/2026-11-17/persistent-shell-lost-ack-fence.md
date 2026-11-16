# Fence delayed PTY creation after a lost acknowledgement

The earlier database-backed shell creation fence covered a PTY request only
when the client received its response before releasing the `agents_meta` row
lock. Review of PR #8978 found the missing failure mode: the client can time
out after it sends `new`, commit the consumed counter without a TTL row, and
then let a terminal transition and resurrection both complete while the PTY
service still owns the accepted request.

Terminal cleanup now asks the local PTY service to acknowledge an agent-local,
exclusive session-index cutoff before it deletes deadline rows. The service
shares its creation lock with that acknowledgement. At acknowledgement, every
earlier request is either already registered and selected for closure, or is
subsequently refused because its numeric shell id is below the retained cutoff.
The cutoff remains through resurrection for the current PTY service process.
If that process stops, it closes all PTYs and ends its request executors before
a new service can admit sessions, so no pending old request survives the
process boundary.

The regression starts a real PTY service request, forces a client-side lost
acknowledgement, commits terminal then resurrection state, and only then lets
the old server request create. It verifies the fence closes that old PTY, leaves
no TTL row, and admits the next higher-id session.
