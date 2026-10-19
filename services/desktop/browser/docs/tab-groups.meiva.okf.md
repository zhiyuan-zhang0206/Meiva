---
type: doc
title: Per-agent tab groups
description: One Chrome tab group per agent, why a closed group becomes saved residue, and what stops it.
tags: [browser, lifecycle]
---

# Per-agent tab groups

Every page an agent creates is filed into the tab group titled `Meiva #<id>`
(`tab_groups/grouping.py`, extension worker `extension/group-*.js`). The title
is the whole identity: nothing maps an agent to a group id, so a daemon restart
or a Chrome restart needs no repair.

## Why a closed group is not a deleted group

Chrome keeps a group whose last tab closes as a **saved** group on the bookmarks
bar (measured on Chrome 155: the chip reads `Meiva #<id> group - Closed`). Saved
groups are outside the extension API: `chrome.tabGroups.query` returns only open
groups, `query({saved: true})` is rejected, and the namespace exposes no delete.
So the title lookup above cannot see one, and the next page for that agent would
create a second group -- one more closed chip per open/close cycle, and saved
groups sync into the signed-in user's own Chrome.

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
