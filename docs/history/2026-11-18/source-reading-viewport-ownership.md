# Source-qualified positions keep their viewport owner

The refresh-reading phase combines the source window API with per-tab locator
metadata. The parked reading visit owns source pages and qualification; the
existing timeline viewport owns materialization, expansion and pixel placement.
The live timeline keeps its request/application, reconciliation and run-state
owners. This avoids making execution depend on optional UI bookmark lookups.

The split matters at two observed boundaries. A qualified source could resolve
a folded child to its parent strip, even though the actual child was absent.
The required locator now opens that child through the existing expansion owner
and waits for its exact node. A second Home consumer control reached an accepted
live head with stable overlap but lost its reading offset when an older prefix
was absent. Joining therefore carries the last measured source member and
offset into that same restore owner, with writes gated until it lands.

Failure preserves the previous durable record. Cancellation retires the local
geometry command as well as the request, and a late completion cannot revive
it. Explicit Retry and reader navigation can establish a new position. Source
budget exhaustion remains an availability reason, rather than evidence that a
message was deleted.

Self-review found that a parked current page can retain plain coordinates while
a newer page enters a new native current domain that reuses them. The direct
consumer reproduced accepting this ambiguity. The approved response is a
conflict guard before page replacement, followed by explicit Retry using the
last measured source locator and offset. The source returns the new retained
prefix and cursors; the client performs no index/rank rebasing. A compaction
boundary may therefore need Retry, while original pages and metadata remain
readable and unchanged until that fresh anchor succeeds.

The released a92 candidate contains the independently reviewed switch/readiness
test fixes. This Home phase requires its own composed-source, browser reload and
latest-message acceptance; the earlier release does not establish those results.

The native browser fixture uses three deterministic replies with 36 text blocks
each to cross the existing 100-row virtualization threshold. Its actual Saver
and HTTP source supplies every locator. Browser acceptance compares source
identity and the measured offset across a same-tab reload, records CSS-pixel
error, and requires the newest reply to replace the prior saved locator. Changing
bookkeeping timestamps is permitted. Other harness sources retain their own
source-level acceptance rather than inheriting this native browser result.
