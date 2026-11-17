# A scheduled update belongs to the host OS

A scheduled update must survive the loss of the message plane it stops. The host
OS therefore owns the complete attempt; Gateway schedules and background agent
shells cannot own this lifetime. Submission validates and warms the release while
services remain live, and persists the fixed target and explicit initiator.

The deadline is one strict attempt. Immediately before stop, any running agent
aborts the window with messages to the initiator and busy actors. No inbox lock,
repeated drain loop or automatic rollback is added: actors coordinate a new
attempt from the reported result. Cancellation shares a lock with execution so
it cannot unload the supervisor of an in-flight update.

This follows the failure boundaries recorded in postmortems 0005 and 0012.
The OS scheduler integration is verified with executable boundary doubles; live
production updates remain a separately authorized operator action.
