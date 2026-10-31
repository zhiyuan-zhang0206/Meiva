---
type: doc
title: Timeline card chrome
description: The hover-revealed chrome of a timeline block — the item timestamp and Copy, its band in the gap below the block, and the hover, focus, touch and expansion rules.
tags:
- frontend
---

# Timeline card chrome

`components/timeline/row.tsx` — `TimelineRow` — renders three visual tiers:
1. **Human message** (`inbound_chat` with `inboundKind(source) === "human"`): a right-aligned conversational bubble (`data-testid="user-message-bubble"`, `max-w-[85%] sm:max-w-[min(70%,34rem)]`, stripped `<header>\n\n` envelope) with a right-aligned hover chrome band (`CARD_FOOTER_CLS`) below it.
2. **Agent reply** (`agent_chat` with `user_facing !== false`): unboxed Markdown prose directly on the canvas (`data-testid="agent-message-prose"`, with a subtle pulsing `streaming-cursor` while `partial` is true) and a left-aligned hover chrome band (`CARD_FOOTER_LEFT_CLS`) below it.
3. **Secondary work item** (thinking, tool call, tool output, system prompt, attach, notes, inter-agent / system inbound, and non-user-facing `agent_chat` with `user_facing === false`): a borderless `MessageCard` + `CardHeader` (`ChevronRight` + icon + summary/title) inside a `TurnBlock` or the Live Activity Window. Secondary items omit below-card timestamps and mount an inline top-right Copy button only when `config.copy` is true (inter-agent / external-agent messages and non-user-facing `agent_chat`).

Copy is **decided per block kind**, in `messageCardConfig`'s `copy` field — a
whitelist, not "everything except …", so a kind added later has to opt in rather
than inherit a button. Only the two kinds whose payload is a message a reader can
quote offer it: `inbound_chat` from a human or an agent (a peer or an external
agent, with human copy payload stripped of its envelope header via `copyPayloadFor`)
and the agent's own reply (`agent_chat`). A system-generated inbound —
scheduler, watcher, framework — is not text a person or an agent wrote and
carries none. Thinking, tool calls and their output, the system prompt,
attachments and system notes are the agent's machinery, not its conversation, and
mount no copy control anywhere in the block: the band's button and the
body-level one are the same decision (`PythonCode` gets `copy={false}`, the
code-output envelope has none), and a body-level button is no longer a way to
"keep" Copy on a kind the whitelist excludes. A still-streaming primary reply
keeps its stamp (`justify-start`) but never Copy. A kind that offers neither Copy
nor a stamp mounts no band at all, so there is never an empty strip under a block.

For primary messages, the band **sits symmetrically in the 20px gap below the
block, outside the message box**, so the time reads as a clean separator between
blocks instead of eating the message's own bottom padding. `.timeline-item` owns
that reservation: it pads its bottom by `1rem` (16px) and hands the height back
as a `-1rem` negative margin (`[data-virtual-group]` adds `py-1` around each
group on top of `space-y-3`, yielding uniform 20px block-to-block spacing and
symmetric ~5.5px visual clearance above and below the 12px chrome band). Drawing
the band inside `.timeline-item`'s own padding keeps it unclipped by
`content-visibility: auto` paint containment; inside `[data-turn-id]`, child
`.timeline-item` rows reset `padding-bottom: 0; margin-bottom: 0` because they
carry no below-card timestamp band. The stamp's rect must stay inside every
clipping ancestor; the Playwright check in
`tests/e2e/visual/test_work_block_chrome.py` verifies both the primary message's
unclipped stamp and the absence of below-card chrome inside `[data-turn-id]`.

The stamp carries no brackets (`2026-05-06 16:56:29`, not `[2026-05-06 …]`) and
renders at `text-[9px]`; Copy is a `size-2.5` icon with no padding, so the control
fits the 12px band instead of overlapping the message edge it sits below, and
reads no taller than the stamp beside it (user 2026-10-22 — at `size-3` the glyph
was visibly bigger than the time). Its hit area is an invisible `after:-inset-1`
box, not a painted one.

## Reveal rules

| State | Band |
| --- | --- |
| Pointer over the block (`group-hover/msgcard`) | visible, interactive |
| Focus anywhere inside the block (`group-focus-within/msgcard`) | visible, interactive |
| Neither | `opacity-0` and `pointer-events-none` — not painted, not clickable, and still reachable by keyboard (focusing Copy reveals it) |
| Touched on a coarse pointer (`TimelineRow` holds the band up for 6 s) | visible, interactive; hidden again after the window |

`opacity: 0` alone still hit-tests, so the hidden state gates `pointer-events`
too. The hidden chrome deliberately stays in the accessibility tree: a screen
reader does not hover, and removing it would take Copy away from every non-pointer
user. A touch device has no hover, so touching a block is its reveal gesture: the
band comes up for `TOUCH_REVEAL_MS` and drops again, instead of standing painted
under every message for good (user 2026-10-22 — the always-visible band was the
bug). A mouse press does not take that path.

```meiva-contract
id = "card-actions-overlay"
kind = "invariant"
statement = "A primary timeline message's timestamp and Copy stand in the gap below the message (right-aligned under user bubbles via CARD_FOOTER_CLS, left-aligned under agent prose via CARD_FOOTER_LEFT_CLS), outside the message box and inside `.timeline-item`'s 1rem bottom padding / -1rem margin hand-back so block-to-block spacing stays uniform and the stamp stays unclipped inside every clipping ancestor. Secondary items inside TurnBlocks omit below-card timestamps and offer Copy only for agent-authored inbound_chat; Copy is scoped to settled message text (human/agent inbound_chat, agent_chat), and the chrome is invisible and click-through until hovered, focused, or touch-revealed."
code = ["ui/web/src/components/timeline/row.tsx:TimelineRow", "ui/web/src/components/timeline/card.tsx:CARD_FOOTER_CLS", "ui/web/src/components/timeline/card.tsx:CARD_FOOTER_LEFT_CLS"]
tests = ["ui/web/src/components/timeline/view/timeline-copy-and-boundaries.test.tsx::Copy is scoped to message text (per-kind whitelist)", "tests/e2e/visual/test_work_block_chrome.py::test_last_block_timestamp_is_not_clipped"]
```

Parent: [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]].

### Waiting for model output

An empty `llm_keepalive` creates a transient, visible waiting card for its
message position when no real block exists. It uses the reasoning card's live
clock presentation with a distinct "Waiting for model" label and empty body;
it is not a reasoning summary or a counted reasoning block. Repeated heartbeats
keep the same item reference and never append text. Real content, terminal
events and committed snapshots remove the waiting card.
