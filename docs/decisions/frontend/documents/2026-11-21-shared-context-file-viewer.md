# The shared-context panel is a file viewer

## Context

The right-hand panel showed one thing at a time: the conversation's `to_user.md` (with a chain of
linked Markdown files), one workspace file reached by clicking a link, or the inspector. A file
could only be reached through a link; the viewer kept its own in-memory Back stack, outside the
browser's history; videos from the gateway never played on a page served from another origin (the
page CSP had no media-src, so default-src rejected them), and remote files could not seek (no Range
support). On 2026-11-20 the user asked for "a feature to view files flexibly, not only a panel",
chose all three proposed batches at once (no CSV table rendering), a tree that can browse other
agents' workspaces, and kept the inspector behind its toggle.

## Decision

- The panel is a tabbed viewer: the conversation's to-user document is the first, permanent tab;
  every file opened in that conversation is a closable tab.
- Every file on show is in the address (`view`, `view_agent`, `lines`, after the to-user chain).
  Opening or selecting a file is a browser history entry; Back and Forward move between files; a
  reloaded or shared address opens the same file. The viewer has no history of its own.
- Quick open searches file names in the workspace the tree shows, opens a typed path as is, and
  lists recently opened files.
- A file tree beside the viewer lists the conversation's workspace; typing another agent's number
  browses that agent's, with the read access links already have. The gateway gains
  `/files/list` and `/files/search` under the same resolution, boundary and cross-machine rules as
  `/files/content`.
- The viewer can fill the page in place of the conversation (desktop); the sidebar stays.
- Audio and PDF render in place; `/files/raw` answers Range requests on both the local and the
  forwarded path; the page CSP admits media from the gateway.

## Alternatives rejected

- **A separate `/files` route.** A page of its own duplicates the panel and its history; filling the
  page with the same panel keeps one viewer and one address scheme (the app directory is also at
  its entry budget).
- **Inspector as a tab.** The user kept it behind its toggle.
- **CSV as a table.** Not wanted; CSV stays text.

## Consequences

- `useWorkspaceFileHistory` (mounted once by the home view) is the only binding between the
  viewer's store and the page history.
- The tab list is per conversation and lives for the tab's lifetime; it is not persisted.
