---
type: doc
title: Inspector mode over the right column
description: What the magnifier switches, what the bar keeps, what stays mounted, and how the narrow reading view and its own scrollbar behave.
tags: [frontend]
---

# Inspector mode over the right column

The inspector is a mode over the right column, not a peer tab: the magnifier at
the end of the document bar switches the column to the inspector (its own queries
and ordered section list) and the same pressed button is the way out. The mode
owns only that column — the agent rail stays mounted, because the mode changes
what the panel shows, not whether navigation is reachable (user 2026-10-20). The
bar keeps just the controls the mode uses: its name, the pressed magnifier and the
column's collapse; the document's home, back and refresh read the body the mode
hides, so they leave with it. The question is not part of this mode: it is the column's own bottom region, so
it stays under the Inspector body with its draft, and switching back restores
the shared document reading position (2026-10-30; until then the mode hid the
question with the body it used to sit in). The panel itself never scrolls: Inspector keeps its
own scrollbar under the same bar. Below the
desktop breakpoint the shared context opens as a full-screen reading view from
the header's To user action — available whenever an agent is selected. Its top
bar pairs Back to messages with the Inspector mode toggle on the right. The
toggle stays in that bar when active and becomes the exit. In narrow Inspector
mode, the document bar disappears entirely and Inspector starts directly below
the top bar. The document component stays mounted to retain reference navigation.
The narrow document toolbar carries the document's own controls; the
mode switch stays in the top bar beside Back.

The reading area, its title bar and the question below it belong to
[[ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md|shared user context]].
