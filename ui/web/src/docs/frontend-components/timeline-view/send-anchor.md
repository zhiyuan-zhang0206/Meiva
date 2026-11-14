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
opening a live work block goes to the tail. Automatic expansion follows the
current reading mode.

During a send and while the agent is working, a reserve below the conversation
lets the reader align the last human message, including after sending from
history. As the reply grows it uses that space before extending the scrollable
end. Tail-following measures the natural end separately from the reserve.

When a turn completes, its remaining reserve keeps the current layout. Once
the reader scrolls upward to or past the natural end, the timeline removes the
reserve below the viewport in place. The reader can go down to the natural end
by scrolling or using the arrow. A new send or working turn can provide space
again. Agent switches remember both the reading position and whether the
reserve is still available. Opening an idle conversation for the first time
uses the natural end.

## Relationship to Other Nodes

- [Timeline sticky scroll](sticky-scroll.md) — the controller and the timeline's scrollport.
