---
type: doc
title: Per-agent tab groups
description: One Chrome tab group per agent per window, how it self-heals across restarts and updates, and why a closed group becomes saved residue.
tags: [browser, lifecycle]
---

# Per-agent tab groups

Every page an agent creates is filed into the tab group titled `Meiva #<id>`
(`tab_groups/grouping.py`, extension worker `extension/group-*.js`). The title
is the whole identity: nothing maps an agent to a group id, so a daemon restart
or a Chrome restart needs no repair.

## The invariant

**An agent's pages sit in exactly one group per Chrome window.** Chrome groups
are window-scoped -- a group cannot span windows -- so "one group per agent" is
only ever true within a window, and this code never moves a tab to another window
to make a title unique. Two windows therefore show two groups with the same
title, which is the intended state, not a duplicate.

Duplicates inside one window are the defect. Every entry point **converges**
instead of assuming, and convergence reads Chrome's own state, so it never
depends on something remembered that a restart could lose:

1. list the open groups with the agent's title **in that window**
   (`chrome.tabGroups.query({title, windowId})`);
2. keep one: the group with the most tabs, ties going to the lowest group id
   (a function of the state alone, so the choice is stable and the second run of
   any pass has nothing left to do);
3. move every other group's tabs into it (`chrome.tabs.group`). Chrome deletes a
   group when its last tab leaves it, so the emptied duplicates disappear with
   the move -- nothing here deletes a group;
4. report what it found and did (the group ids, the keeper, how many were merged,
   how many tabs moved), which the daemon logs as a WARNING.

A tab that already sits in a group this stack did not make is left alone: a page
the user filed into their own group is theirs, and grouping is presentation only.

### Triggers

| Trigger | Where |
| --- | --- |
| every page creation | `meivaGroup(targetId, title)` converges that tab's window before filing (`AgentTabGroups.group_new_page`) |
| daemon start and every upstream reconnect | `AgentTabGroups.converge_all()` -> `meivaConvergeAll()` (`mcp_daemon._serve_generation`) |
| Chrome start and extension update/install | `chrome.runtime.onStartup` / `onInstalled` call `meivaConvergeAll()` inside the worker |

All four run the same stateless pass, so they compose: a restart between them
cannot leave a second group behind, and a run with nothing to repair changes
nothing. The worker keeps no state -- the service worker is stopped after ~30s
idle and restarted by Chrome at will, and none of that has to be remembered.

Measured on Chrome 153 (Linux, throwaway profile, real extension): two groups of
one tab each in one window converge to one group holding three tabs; the same call
again reports `merged: 0, moved: 0` and leaves the browser untouched; a page
created in a second window creates that window's group and does not move the first
window's tabs.

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

Before this was understood, one agent (6866) accumulated three closed groups.

## The rule

The daemon ungroups an agent's **last** tab *before* closing it
(`AgentTabGroups.release_page`, called from `ChromeMcpDaemon._call` for every
`close_page`). An ungrouped last tab deletes its group instead of saving it
(measured), so the stack never adds a closed chip of its own. The decision about
which tab may be ungrouped lives in the extension (`meivaUngroupLast`): the tab
must be in a group whose title is exactly this agent's and that group must have
no other member, so a group the user made is never touched.

Both halves are best-effort and warn only: a page that must close still closes,
and a failure to ungroup leaves a chip rather than blocking the close.

## What this cannot fix

A tab the **user** closes by hand never passes through `close_page`, so Chrome
saves that group and nothing in this stack can remove it. Those chips are deleted
from the Chrome UI. Reopening one is Chrome's own action (`Open group`); this
code never opens or deletes a saved group.

## The extension's toolbar entry

The manifest declares no `action`, so the extension has **no toolbar button**: the
worker confirms `typeof chrome.action === "undefined"` and Chrome lists `action` as
`null` for it (measured on Chrome 153). It appears only in the extensions menu
(the puzzle-piece list), which lists every loaded extension and cannot be hidden;
the daemon loads it lazily and Chrome keeps it loaded for the session.
