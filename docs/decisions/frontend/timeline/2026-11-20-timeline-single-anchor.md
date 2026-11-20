# Timeline position: one anchor, one writer

## Context

The 2026-07-15 decision ([sticky single writer](2026-07-15-sticky-single-writer-controller.md))
made one controller own the *follow flag*. Every position feature added afterwards
owned its own piece of the *position* instead:

- bookmark restore with hold/settle/probe;
- the reload bookmark search;
- the send anchor and its reserve;
- the prepend anchor;
- the compact re-key anchor;
- the width-change re-anchor;
- the virtual-window preserve;
- the reveal;
- the bottom ride;
- the header-toggle anchor.

By 2026-11-20 thirteen code paths wrote `scrollTop`. Their only arbitration was
effect order, and one hold flag was cleared from about six places. About 30 commits
since 10-19 patched interactions between them, and four behaviours flip-flopped.

Frame captures on production (`bff650c6733a`) measured the result:

- **Every return to a visited agent** painted the retained rows at `scrollTop 0` for
  0.3–1.0 s, then jumped to the bookmark. The restore waited for the authority read
  while the rows were already painted.
- **First visits** painted a 1–3 row page top-aligned, then reflowed and jumped when
  cold fill landed.
- **Reload** showed the tail under the ring while it searched for a deeper bookmark.
- **Code reading**: a revisit that crosses a disjoint snapshot gap restores raw pixels
  into unrelated rows.

## Decision

**One rule:**

1. The viewport position is one value per agent, the `ViewAnchor`:
   - `tail`, or
   - `row` + `offset`: the row's top edge sits `offset` px below the viewport top.
2. One writer applies it after every commit and every layout change (ResizeObserver
   on the viewport and the content).
3. Only reader input derives a new anchor from the DOM: wheel, touch, keys,
   scrollbar, or any scroll the writer did not cause.

Consequences of the rule:

- **Programmatic placements** (first visit, saved return, reload, reveal, quote,
  send, header toggle, the bottom button) only *set* the anchor.
- **Layout changes keep the anchor row still**: prepends, compaction, width, images,
  late heights, virtual spacers. There is no per-cause compensation code.
- **Never paint a non-destination frame.** Content is visible only while its anchor
  resolves in the loaded rows:
  - retained rows that contain the saved row are placed in the first frame;
  - otherwise the rows stay hidden under the one ring until the anchor resolves (by
    id, then by `reading_locator`) or falls back to the tail;
  - the authority read and later merges only re-resolve, so a surviving row never
    moves.
- **Short first pages and the reserve**:
  - a first page shorter than the viewport stays hidden while the cold fill reads
    its (at most two) older pages, so history never lands above rows already on
    screen; short conversations keep the top-aligned column (a bottom-aligned one
    left a large blank above them, which the user questioned in Preview);
  - extra space below the content (the reserve) is part of an anchor that a send or
    toggle placed, and is released when the reader scrolls above the natural end.
- **Saved positions** are the anchor itself:
  - per tab entry in memory, and in `sessionStorage` for reload;
  - a departure from the tail saves a row (2026-10-17 ruling);
  - reload at the tail stays tail.

## Alternatives rejected

- **Keep patching the writers.** Each fix moves the flash elsewhere; the measured
  return flash is itself asserted by a unit test.
- **Keep recent timelines mounted but hidden.** Returns would be exact without a
  restore, but reload still needs the anchor machinery. It costs N live DOM trees
  that go stale without their streams. Once one writer exists, remounting is
  already flash-free, so this would be a second mechanism for the same job.
- **A parked reading window detached from the live tail** (reverted #9017). It
  blocks the live stream and needs UI to leave it.

## Consequences

- `bookmark-restore`, `restore-settle`, the prepend, compact, width and send-arrival
  anchors, the sticky follow flag and the viewport listener hook collapse into the
  anchor module.
- Unit tests assert positions across transitions, not internal flags. E2E samples
  every painted frame of a switch or reload and fails on any frame that is neither
  the source nor the destination.
- A first visit whose first page does not fill the viewport waits for the cold fill
  under the ring: zero reflow at the cost of up to two older-page reads of latency.

Later the same day the send park and its reserve were removed ([a send moves nothing](2026-11-20-send-moves-nothing.md)); the toggle reserve remains.
