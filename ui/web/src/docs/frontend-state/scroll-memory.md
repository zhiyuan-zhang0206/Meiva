# History-Entry Scroll Memory

The document never scrolls (h-full chain; every page scrolls inside its own
element), so the browser's history scroll restoration has nothing to restore:
a back/forward remount would reset each container (timeline re-pinned to the
newest message, terminal to its tail). Every scrolling surface saves its
position under the history entry it belongs to — keyed by
`router.bfcacheId`, which the client router keeps across back/forward,
`router.refresh()`, and search-param-/hash-only navigations and hands out
fresh only when a push/replace lands on a new segment (vendored Next 16.3.4
use-router docs), independent of `cacheComponents` — so a restore happens
only on a return to a kept entry; a first visit keeps its own default, and a
search-param-only switch (e.g. `?agent_id=`) shares the previous home
visit's slot. The record carries a content key (agent / agent+session): a
mismatch drops the position instead of restoring it onto other content.
The home timeline keeps its own place record under the same entry key
(`components/timeline/anchor/saved.ts`). A reader who left in history returns
to their row and viewport offset, within the available scroll range.
A reader who left at the tail keeps the latest human message or visible reply
as a watermark. On return the timeline opens at the tail bounded by unread
content, as described in [Timeline sticky scroll](../frontend-components/timeline-view/sticky-scroll.md).
Reaching the natural end or using the down arrow resumes following.

When `feature.reloadReadingRestore` is enabled (default `true`), the home
timeline also persists a bounded per-tab bookmark map in `Browser.session`
(`meiva.timeline.reading-bookmarks.v1`, capped at 64 agents with a 7-day TTL)
holding the same fields plus `scrollTop` and `savedAt`. Same-page switches prefer
the in-memory place. A full page reload opens on the session bookmark. Older
bookmarks with extra fields still restore their row and offset. Following
the tail clears it, so a reload from the tail opens on the latest rows. Turning the feature off bypasses all `Browser.session` reads
and writes. Terminal panes keep the shared `scroll-memory.ts` record, whose
`followBottom` flag serves their tail following.


A saved place also records the timeline response's `current_epoch` (`epoch` on the
place and on the bookmark). A harness switch re-labels every row of the ended epoch
with an `e<seq>.` prefix, so when the place's epoch is older than the current one the
placement machine looks for `e<epoch>.<id>` (rank 0) instead of the id as saved; the
older-page search then reaches it through the epoch pages. A place whose id is already
prefixed, or a gateway that sends no `current_epoch`, is used as saved, and a row that
never arrives falls back to the tail as before.

Parent: [Frontend State Management](frontend-state.md).
