# Reading from the latest human message

A successful send parks its committed human message at the reading offset below
the header (44 px plus a 12 px gap) when the reader is following the tail. A
reader in history keeps their position. The request carries the receipt's
inbound id so a later timeline update cannot park an unrelated reply. A scroll
away while the row is arriving takes precedence over automatic positioning.

The centered down arrow jumps to the latest visible human message when that
message is below the reading offset. The window mounts that row first if it was
virtualized. From the reading offset or farther down, the arrow jumps to the
current content end and resumes following when the end is farther down. If the
reader is already in the additional space below the content, the arrow uses
the remaining scroll range. The arrow is hidden at the scrollable end,
including when the last human message is aligned using that space. Manually
opening or closing a work block, live or settled, keeps its header where it
is (user ruling 2026-11-17). Automatic expansion follows the current reading
mode.

During a send and while the agent is working, a reserve below the conversation
lets the reader align the last human message, including after sending from
history. The reserve is exactly the space the current row anchor needs to sit
at its offset, so as the reply grows it uses that space before extending the
scrollable end, and the tail never carries any. Tail-following measures the
natural end from the content itself.

Remaining reserve keeps the current layout through streaming and completion.
Once the reader actively scrolls upward to or past the natural end, the timeline
removes that reserve in place, including while the agent is working. The boundary
is the natural content height minus the viewport height, clamped to zero for a
short conversation. Upward movement inside the reserve keeps it available;
layout changes alone do not spend it.

The reader can then go down to the natural end by scrolling or using the arrow.
Only a new successful send renews released space. Sending from history renews
that space while keeping the reading position. Stream growth, working-state
changes and agent switches preserve the released state. Agent switches remember
both the reading position and whether reserve is still available. A first visit
to a working conversation can provide space; a saved released position takes
precedence. Opening an idle conversation for the first time uses the natural end.

## Relationship to Other Nodes

- [Timeline sticky scroll](sticky-scroll.md) — the controller and the timeline's scrollport.
