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
Conversation switches always save the visible message and viewport offset,
including when leaving the tail. Returning restores that reading position and
keeps it anchored through subsequent output. First visits keep their default
bottom placement. Scrolling or explicitly navigating to the bottom resumes
following.

The shared record still supports explicit `followBottom` bookmarks and terminal
following. The timeline save path writes false for both agent switches and
history-entry remounts; bfcache identity chooses the bookmark, not a separate
following policy. Restores land in a layout effect on the first commit that can
hold them (before paint), and the mount force-pin defers to a pending restore.


Parent: [Frontend State Management](frontend-state.md).
