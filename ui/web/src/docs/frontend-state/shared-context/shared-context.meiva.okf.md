---
type: doc
title: Shared user context
description: Workspace document reading, notification interaction and Inspector layout beside agent messages.
tags:
- frontend
---

# Shared user context

The home page displays the selected agent's workspace document beside its message
stream. Desktop gives the narrow agent tree 16% by default, then divides messages
and the shared-context panel 4:3. The document is the panel's body — notifications
tuck in below it as the column's bottom region — and it stays present for every selected agent: a missing or
empty document shows the empty state and a legacy `to_user.html` shows the
migration placeholder — an empty document never collapses the panel. The column
hides only when the user closes it: `display.context_panel_open` (default on) is
the title bar's PanelRight toggle, and the timeline takes the width until it is
reopened. The panel itself is a lazy boundary, loaded when the right panel first
renders.

The document body is the column's reading area: its `ScrollArea` viewport owns
reading-position capture and restoration across Inspector mode changes, and the
body has no nested scrollbar. The question is the column's own bottom region,
pinned under that area instead of travelling inside it (user report 2026-10-30,
replacing the 2026-10-29 order in which it was the reading area's first block).
The column therefore reads title bar, reading area, question: the question keeps
its place while the document scrolls, and one taller than the region's `70dvh`
cap (applied to the inner `ScrollArea` viewport so overflow scrolls rather than
clipping against an `auto`-height root) scrolls inside its own card rather than
starving the reading area.
Question controls use the timeline's 13px body size and compact row spacing.
Touch inputs retain their 16px baseline; a narrow desktop column still uses 13px.
The document bar is the right column's title bar, outside the reading viewport
in both modes: only the reading area scrolls, never the bar (user 2026-10-29), and
the bottom region never pushes that bar out of the column. There is no
separate notice bar, queue counter or collapse control. Document padding supplies the initial spacing;
the first Markdown block adds no top margin. Within `[data-testid="document-content"]`,
`ChatMarkdown` uses a compact auxiliary scale (`13px / 21px` prose in softened
dark `#dfdcd6`, `15px / 14px / 13px / 12.5px` headings, `12px` code blocks and
tables, and `11.5px` inline code) so the right-hand workspace document stays
visually subordinate to the `15px` main conversation column. Plain numbered choice rows avoid a card around every option.
A persistent comment input and the shared send button match the chat composer;
the selection and comment form one explicit submission, with Dismiss as a
secondary ghost action below the reply input. This keeps the user's task in one reading flow rather than
splitting it into stacked control panels.
Switching agents or navigating to another referenced document resets this shared
reading position to the top.

The inspector is a mode over the right column, not a peer tab: the magnifier at
the end of the document bar switches the column to the inspector and the same
pressed button is the way out. What the mode keeps mounted, what the bar carries,
and how the narrow reading view and its own scrollbar behave have their own node:
[[ui/web/src/docs/frontend-state/shared-context/inspector-mode/inspector-mode.meiva.okf.md|Inspector mode]].

`["to-user", agentId]` owns the root document snapshot. Linked Markdown uses
that query-key prefix followed by its reference chain. The home-machine ops
server reads `to_user.md`; there is no format setting, HTML frame, publication
registry or database content copy. `ChatMarkdown` owns typography, GFM, CJK
behavior and safe links for both conversation and document surfaces. The document
explicitly skips raw HTML. External links open in a new tab; linked Markdown
opens in the panel and is pushed into browser history (`?agent_id=…&reference=…`),
so Back and Forward walk the same chain and a reload or a shared link restores
it; [panel navigation history](../navigation-history.meiva.okf.md)
owns that contract. Other files open or download through the authenticated gateway resource
endpoint.

The document response maps Markdown link/image destinations to resource IDs.
The backend parses Markdown with markdown-it-py, including reference-style links;
code fences and raw HTML do not expose resources. Relative paths resolve from
the containing Markdown file, absolute paths and `~/` paths resolve on the
agent's home machine. `file:` URIs with an empty or localhost authority work too.
Root-relative paths denote local files; use a full HTTP(S) URL for a web route.
There is no required assets directory, file copy or agent-owned HTTP server.

The browser submits only a chain of reference IDs. Every read traverses that
chain again from `to_user.md`, checking each link in the current parent document.
Deleted references stop resolving. Symlinked resources resolve to regular files;
the root document and workspace reject symlinks. Filesystem access runs under
the runner's OS identity. External URLs are never fetched by this resolver.

`POST /to-user/resource` on the existing authenticated ops listener transports
bounded binary bytes, using the ops worker pool, admission and shutdown accounting.
The gateway authenticates the user and isolates evaluation readers before resolving
the owner machine. Both ends enforce the configured per-resource bound
(`settings.to_user.max_resource_bytes`, 64 MiB by default). The current transport
buffers a bounded resource; it does not support ranges or large media streaming.
Markdown stays capped by the configured `settings.to_user` limits — 1 MiB per
document, 1024 local references and 16 linked-document hops by default. The
response that serves those files renders HTML inline in a script-only sandbox
that keeps an opaque origin, media, PDFs and inert text inline under the plain
sandbox, and archives, office documents, executables and unknown types as
attachments; [to user resources](../../../../../../gateway/agents/docs/to-user-resources.meiva.okf.md)
owns that boundary and its contract. Every resource response carries nosniff and
is never publicly cached.

One shallow scanner per ops process polls numeric workspace roots and only their
immediate `to_user.md` targets, on a worker thread. Creation, atomic replacement
and deletion broadcast `to_user_changed`; the global fold repairs that agent's
query prefix and includes it in reconnect repair. It does not recursively watch
linked directories. Refresh/focus rechecks referenced file stamps, so same-name
attachment replacements receive new versioned URLs even without a body edit.

Agent workspaces seed an empty `to_user.md` at boot, so the panel surface exists
from the agent's first turn; until the agent writes, the document area shows its
empty state (or the legacy placeholder), never nothing. An existing legacy `to_user.html` is preserved and
reported as awaiting conversion, never rendered. Agent workspace guidance tells
agents to rewrite their current shared context into Markdown. Removed stored
format pins are ignored by the existing config-pin resolver.

A failed read retains an already-cached snapshot and marks it stale; it never
turns a machine failure into an empty file. Files must remain available on their
source machine. This is not an offline document store or an archival snapshot.

On desktop the home page mounts the panel for every selected agent (the document
area owns its empty and legacy states); on narrow viewports the header's To user
action opens the same panel as the reading view.
