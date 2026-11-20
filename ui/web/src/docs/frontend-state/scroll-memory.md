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
The home timeline keeps its own place record instead
(`components/timeline/anchor/saved.ts`): the view anchor of the row on screen
(`itemId`, `rank`, whether it is a whole turn, the viewport offset, its
`reading_locator` and reserve flags) under the same entry key. Conversation
switches always save it, including when leaving the tail. Returning places that
row in the first painted frame and keeps it anchored through subsequent output.
First visits keep their default placement. Scrolling or explicitly navigating
to the bottom resumes following.

When `feature.reloadReadingRestore` is enabled (default `true`), the home
timeline also persists a bounded per-tab bookmark map in `Browser.session`
(`meiva.timeline.reading-bookmarks.v1`, capped at 64 agents with a 7-day TTL)
holding the same fields plus `scrollTop` and `savedAt`. Same-page switches prefer
the in-memory place; a full page reload opens on the session bookmark. Following
the tail clears it, and so does a send parked from the tail (the reader is
following their own message, so a reload opens on the latest rows). Turning the feature off bypasses all `Browser.session` reads
and writes. Terminal panes keep the shared `scroll-memory.ts` record, whose
`followBottom` flag serves their tail following.


Parent: [Frontend State Management](frontend-state.md).
