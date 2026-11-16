# Restore the home conversation's older-history feedback

A reader reported no loading ring after reaching the top, pausing and scrolling
up again on desktop. The home conversation surface forwarded `loadingOlder` but
omitted `readerLoadingOlder`, so every real home request reached TimelineView
with its presentation flag at the false default. Direct hook-to-view fixtures
and tests supplying the flag themselves bypassed this consumer and could not
establish the home behavior.

Forward the existing reader-origin value through the home's narrowed timeline
type, destructuring and TimelineView props. Keep origin ownership in useTimeline
and preserve the current gesture threshold. The regression mounts the real
useTimeline, HomePageView/HomeContent and TimelineView with a controlled API,
including reader failure/retry and background failure followed by reader input.
The unchanged base sends a page request and sets store loading while its ring
remains idle; the corrected path presents the ring throughout the reader read.

A separate diagnostic found small wheel steps separated by more than 120 ms
lose partial progress toward 64 px. This change preserves that gesture contract;
the evidence and possible cumulative-intent follow-up remain with task #7070's
coordinator. A no-progress page claiming has_more is exhausted by the existing
store guard and is not equivalent to a failed request that can be retried.
