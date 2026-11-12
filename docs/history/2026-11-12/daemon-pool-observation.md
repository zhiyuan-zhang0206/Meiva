# Attribute daemon cleanup to its invocation

The TTL reaper validation at60a reported one global meiva-tagged backend after
its cleanup deadline. The original output retained no PID or backend-start anchor,
so it cannot establish whether that backend belonged to the daemon. Both TTL and
events-maintenance observers already rolled back before each read; adding another
rollback would not repair the missing ownership information.

Tests now assign a unique gate to the handle they pass to the daemon and check
those sessions separately. The global check compares PID/backend-start anchors
to the baseline so a newly surviving session outside that gate is still rejected.
Pre-existing clients remain visible as baseline evidence rather than being
silently attributed to the invocation. This preserves a global guard without
assuming that every tagged backend is the daemon's pool.

An owned borrowed connection must keep the test red even after pool.close returns;
returning the borrow permits the original bounded cleanup observation to pass.
Foreign-baseline and new-foreign controls distinguish the two assertion boundaries.
The TTL10s and events15s observation deadlines and0.05s interval are retained.
No production pool.close or executor cancellation behavior changes here.

The acceptance owner accepted PG owned anchors/state/waits/query shapes and async
task diagnostics as the test-only boundary. The daemon does not expose its pool
object or Python worker ownership through this seam; diagnostics say so explicitly.
A production pool-factory hook would expand this repair without strengthening the
backend-release assertion, so it is deferred. No such hook is added here.
