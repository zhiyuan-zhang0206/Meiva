# Panel navigation history

The panel's reading position is the URL, not component state:
`?agent_id=<agent>&file=<name>&file=<name>` is the chain from `to_user.md` to
the document on screen. An element is the file's readable name — from the
workspace root, or absolute for a file outside it — and keeps its `/` in the
address bar, so a copied link says which file it opens instead of carrying a
digest. Every in-app navigation — a linked Markdown document,
or the home control — pushes a history entry, so browser Back returns to the
previous document instead of leaving the app and Forward returns to the one just
opened. A reload or a shared link restores the same chain.

`popstate` is the only way a traversal reaches the component: it re-reads the
chain from the location, so the view and the address bar cannot disagree. The
panel's own Back control follows the same semantics — it calls `history.back()`
for an entry this panel pushed (the entry's history state carries the marker) and
otherwise rewrites the current entry to the parent, because a deep link has no
in-app entry to return to. Selecting another agent drops the previous chain; it
belongs to the agent that was open. Selecting an agent is itself a history
entry (user report 2026-10-20): two agents selected in a row read as A -> B, so
browser Back opens A again and Forward returns to B. `?agent_id=` stays the
source of truth and a `popstate` traversal writes the selected agent back into
the store instead of pushing the entry it just restored; only a change between
two selected agents pushes, because the agent the app opens on rewrites its own
entry in place and must not leave a duplicate behind it. External links keep opening in a new tab and
non-Markdown resources keep their own document.

The panel's document chain lives in the URL as `?agent_id=…&file=…`: opening a linked document pushes a history entry, a popstate traversal restores the chain it selects, and the panel's Back control calls `history.back()` for entries it pushed while a chain without history rewrites its own entry instead of leaving the app. Selecting another agent drops the previous chain.

A workspace file open in the viewer is in the URL too (user ruling 2026-11-20), after the chain it
was opened from: `&view=<path>&view_agent=<owner>&lines=<range>`.

- **Opening.** Opening a file, from the panel, a conversation message or a link inside another file,
  pushes an entry. Only a new line range in the same file replaces the entry.
- **Browser Back and Forward.** These move between viewed files and back to the document.
- **Panel controls.** The panel's Back is the browser's Back for entries it pushed. Its Home pushes
  an entry without the file.
- **Closing the panel.** Closing drops the file from the current entry.
- **Opening a link.** A reloaded or shared address opens the same file.
- **Switching agents.** Selecting another agent drops the file, as it drops the chain.

`useWorkspaceFileHistory` (mounted once by the home view) is the binding between the viewer's store
and the page history.
