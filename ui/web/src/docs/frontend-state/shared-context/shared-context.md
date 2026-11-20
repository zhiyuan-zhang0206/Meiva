# Shared user context

The home page displays the selected agent's workspace document beside its message
stream. Desktop gives the narrow agent tree 16% by default, then divides messages
and the shared-context panel 4:3. The document is the panel's body and stays
present for every selected agent: a missing or empty document shows the empty
state and a legacy `to_user.html` shows the migration placeholder. Closing the
panel gives its width to the conversation until it is reopened through
`display.context_panel_open`. `SharedContextPanel` and `InspectorPanel` are
imported statically so mode changes have no separate loading fallback.

The document's `ScrollArea` viewport owns reading-position capture and
restoration across Inspector mode changes. Its title bar stays outside that
viewport. Switching agents or navigating to another referenced document resets
reading position to the top. Within `[data-testid="document-content"]`,
`ChatMarkdown` uses the compact auxiliary scale (`13px / 21px` prose,
`15px / 14px / 13px / 12.5px` headings, `12px` code blocks and tables, and
`11.5px` inline code), subordinate to the `15px` conversation column.

Pending questions belong above the conversation composer, with one collapsed
queue row and answers expanding over the timeline. Their forms, draft lifecycle
and suggestion acceptance have their own node:
[Conversation question queue](questions.md).

The inspector is a mode over the right column, not a peer tab: the magnifier at
the end of the document bar switches the column to the inspector and the same
pressed button is the way out. What the mode keeps mounted, what the bar carries,
and how the narrow reading view and its own scrollbar behave have their own node:
[Inspector mode](inspector-mode/inspector-mode.md).

`["to-user", agentId]` owns the root document snapshot. Linked Markdown uses
that query-key prefix followed by its file chain. The home-machine ops
server reads `to_user.md`; there is no format setting, HTML frame, publication
registry or database content copy. `ChatMarkdown` owns typography, GFM, CJK
behavior and safe links for both conversation and document surfaces. The document
explicitly skips raw HTML. External links open in a new tab; linked Markdown
opens in the panel and is pushed into browser history (`?agent_id=…&file=…`),
so Back and Forward walk the same chain and a reload or a shared link restores
it; [panel navigation history](../navigation-history.md)
owns that contract. Other files open or download through the authenticated gateway resource
endpoint.

The document response maps Markdown link/image destinations to readable file names.
The backend parses Markdown with markdown-it-py, including reference-style links;
code fences and raw HTML do not expose resources. Relative paths resolve from
the containing Markdown file, absolute paths and `~/` paths resolve on the
agent's home machine. `file:` URIs with an empty or localhost authority work too.
Root-relative paths denote local files; use a full HTTP(S) URL for a web route.
There is no required assets directory, file copy or agent-owned HTTP server.

The browser submits only a chain of file names. Every read traverses that
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
response that serves those files returns HTML as an attachment under the passive
CSP without script permission. The workspace viewer renders only Markdown, images
and videos; HTML and HTM open as source code, and other binary files offer download.
Each file the viewer opens is a browser history entry, so Back and Forward move
between viewed files ([panel navigation history](../navigation-history.md)).

The panel is a file viewer (user rulings 2026-11-20 and 2026-11-21; decision record
`docs/decisions/frontend/documents/2026-11-21-shared-context-file-viewer.md`):

- **One bar of tabs and controls.** The bar has the conversation header's height, so their rules
  meet in one line.
  - The to-user document is the first, permanent tab, and it is the panel's home. Every linked or
    picked file, Markdown documents included, is a closable tab. The tab list lives for the browser
    tab's lifetime.
  - On the right sit fill the page, the inspector toggle, and collapse.
  - There is no Home, Back or Refresh button: Back and Forward are the browser's.
- **Auto-refresh.** The file on show is re-read every 10 s while the page is visible; media are not.
- **Picking a file.**
  - The side panel has a path bar like a file manager's address bar (`#63 › project › src › main.py`).
    Each folder drops down its entries: a folder opens in place, a file opens as a tab.
  - The root drops down another agent's number, to browse that agent's workspace with the read
    access links already have.
  - Once the viewer fills the page, the tree takes the path bar's place on the left.
- **Width.** The panel can be dragged far to the left. The conversation keeps a 280 px minimum.
- **Fill the page.** On desktop the viewer can take the conversation's place; the sidebar stays.
- **What renders in place.** Markdown, images, video, audio, PDF (the browser's viewer) and source
  code with line ranges. Other binary files offer a download.
- **Labels.** File names show without a type icon.
- **What a linked file may be.** A file the to-user document links to opens like any other file:
  it must lie in an agent's workspace or the agent's working directory. A linked Markdown document
  outside both, which the earlier document chain rendered through the to-user resource route, shows
  "can't be shown". This narrowing is deliberate (2026-11-21): every tab reads through one route.
Normal Markdown links are directly clickable in conversation messages and `to_user.md`;
explicit `.meiva/workspaces/<id>/...` references keep the viewer attached to the
active conversation's panel while routing content/raw reads and nested relative
Markdown links or images to `<id>`'s workspace across machines.

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

A failed read retains an already-cached snapshot quietly without stale banners;
it never turns a machine failure into an empty file. Cold failures render an
`InlineError` with Retry. Files must remain available on their source machine.
This is not an offline document store or an archival snapshot.

On desktop the home page mounts the panel for every selected agent (the document
area owns its empty and legacy states); on narrow viewports the header's To user
action opens the same panel as the reading view.
