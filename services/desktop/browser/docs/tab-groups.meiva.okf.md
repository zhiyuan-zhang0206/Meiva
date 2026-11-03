---
type: doc
title: Per-agent tab groups
description: One Chrome tab group per agent across the browser, how it self-heals across restarts and updates, how a single staged extension avoids duplicate toolbar entries, and why pre-close ungrouping wakes idled-out workers.
tags: [browser, lifecycle]
---

# Per-agent tab groups

Every page an agent creates is filed into the tab group titled `Meiva #<id>`
(`tab_groups/grouping.py`, extension worker `extension/group-*.js`). The title
is the whole identity: nothing maps an agent to a group id, so a daemon restart
or a Chrome restart needs no repair.

## The invariant

**An agent's pages sit in at most one open group across the entire browser.**
Multiple Chrome windows in the shared browser are accidental edge cases rather
than separate workspaces, so `Meiva #<id>` titles converge globally across all
windows. Every entry point **converges** instead of assuming, reading Chrome's
own state:

1. list every open group with the agent's title across all windows
   (`chrome.tabGroups.query({ title })`);
2. keep one: the group with the most tabs, ties going to the lowest group id
   (a function of Chrome's state alone, so the choice is deterministic and a
   second pass has nothing left to do);
3. move every other group's tabs into it (`chrome.tabs.group`) and keep the
   keeper anchored in its window (`chrome.tabGroups.move`, since cross-window
   `chrome.tabs.group` can otherwise pull the group into the moved tab's window
   — measured on Chrome 156). Chrome deletes a group when its last tab leaves
   it, so the emptied duplicates disappear with the move;
4. report what it found and did (the group ids, the keeper, its window, how many
   were merged, how many tabs moved), which the daemon logs as a WARNING.

A tab that already sits in a group this stack did not make (`!MEIVA_TITLE`) is
left alone, and session-restore layout reapplication keeps non-`Meiva #<digits>`
user groups scoped per window: a group the user made is theirs, and grouping is
presentation only.

### Triggers

| Trigger | Where |
| --- | --- |
| every page creation | `meivaGroup(targetId, title)` converges that title across the browser before filing (`AgentTabGroups.group_new_page`) |
| session layout restore | `meivaApplyLayout(spec)` converges `Meiva #<digits>` groups through `meivaConvergeTitle` (`session_restore.reapply`) |
| daemon start and every upstream reconnect | `AgentTabGroups.converge_all()` -> `meivaConvergeAll()` (`mcp_daemon._serve_generation`) |
| Chrome start and extension update/install | `chrome.runtime.onStartup` / `onInstalled` call `meivaConvergeAll()` inside the worker |

All four run the same stateless pass, and inside `browser-mcp` one shared
`ExtensionWorker` lock serializes `session_restore.reapply`, `converge_all`,
`group_new_page` and `release_page` so two callers never reload the extension
underneath each other.

## Why a closed group is not a deleted group

Chrome keeps a group whose last tab closes as a **saved** group on the bookmarks
bar (measured on Chrome 155: the chip reads `Meiva #<id> group - Closed`). Saved
groups are outside the extension API: `chrome.tabGroups.query` returns only open
groups, `query({saved: true})` is rejected, the namespace exposes no delete, and
a saved group is neither a bookmark (`chrome.bookmarks.getTree()` does not list
it) nor a restore-able session (`chrome.sessions.getRecentlyClosed()` does not
either) -- measured on Chrome 153. So the title lookup above cannot see one, and
the next page for that agent would create a second group -- one more closed chip
per open/close cycle, and saved groups sync into the signed-in user's own Chrome.

## The rule

The daemon ungroups an agent's **last** tab *before* closing it
(`AgentTabGroups.release_page`, called from `ChromeMcpDaemon._call` for every
`close_page`). Because Chrome stops an MV3 service worker ~30s after idle while
leaving open tab groups in the tab strip, `release_page` wakes/reloads the
worker via `ExtensionWorker` (`Extensions.loadUnpacked`) when needed before
calling `meivaUngroupLast`. An ungrouped last tab deletes its group instead of
saving it (measured), so an agent run of any duration leaves zero closed chips.
The decision about which tab may be ungrouped lives in the extension
(`meivaUngroupLast`): the tab must be in a group whose title is exactly this
agent's and that group must have no other member, so a group the user made is
never touched.

Both halves are best-effort and warn only: a page that must close still closes,
and a failure to ungroup leaves a chip rather than blocking the close.

## What this cannot fix

A tab the **user** closes by hand never passes through `close_page`, so Chrome
saves that group and nothing in this stack can remove it. Those chips are deleted
from the Chrome UI. Reopening one is Chrome's own action (`Open group`); this
code never opens or deletes a saved group.

## Single staged extension and toolbar pin state

`meiva-browser` keeps Chrome alive across `--keep-browser` restarts and release
rollouts. Loading directly from each release checkout's `extension/` directory
accumulated one `Meiva Tab Groups` entry per checkout path, each with a new
path-derived extension id. On Chrome 156, even without an `"action"` key in
`manifest.json`, Chrome synthesizes a fallback `M` toolbar item with
`extensionsPinnedByDefault: true`, so every new extension id appeared as another
pinned `M` icon and discarded any prior unpin toggle.

To keep one permanent extension identity:

1. `manifest.json` carries a fixed RSA public `"key"`, giving the extension one
   deterministic id regardless of checkout path;
2. `ExtensionWorker` stages `manifest.json` and `group-<hash>.js` atomically
   into `<home>/run/tab-groups-extension` (pruning older `group-*.js` files) and
   loads only that path;
3. before reusing or loading, `_worker_session` checks `Extensions.getExtensions`
   and calls `Extensions.uninstall` on any `"Meiva Tab Groups"` entry loaded from
   any other path, leaving user unpacked extensions untouched.
