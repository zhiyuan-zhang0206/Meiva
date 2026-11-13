# Reading from the latest human message

A successful send parks its committed human message at the reading offset below
the header (44 px plus a 12 px gap) when the reader is following the tail. A
reader in history keeps their position. The request carries the receipt's
inbound id so a later timeline update cannot park an unrelated reply. A scroll
away while the row is arriving takes precedence over automatic positioning.

The centered down arrow jumps to the latest visible human message when that
message is below the reading offset. The window mounts that row first if it was
virtualized. From the reading offset or farther down, the arrow jumps to the
current content end and resumes following. Manually opening a live work block
also goes to the tail. Automatic expansion follows the current reading mode.

A reserve below the conversation gives the last human message enough room to
reach the reading offset by wheel, drag, touch, or arrow, including after a send
from history and after a short reply ends. Its height is the difference between
the reachable reading position and the natural content height. Reply growth
uses that space before extending the scrollable end. Tail-following measures the
natural end without the reserve, and moving down into the reserve takes the
reader back to a fixed reading position.

## Relationship to Other Nodes

- [Timeline sticky scroll](sticky-scroll.md) — the controller and the timeline's scrollport.
