# Stream reveal

A streaming row shows what a reveal controller has uncovered, not what has
arrived. Providers deliver text in uneven bursts (a Claude thinking summary lands
as one ~130 character burst in 5 ms, Codex text as 3-90 character packets 14-300
ms apart, Antigravity as a sentence per update), and appending each burst as it
comes makes the model's speed visibly jitter. A buffer and a soft leading edge
turn that into a steady flow.

## Arrival and display are separate

The timeline store keeps every received byte exactly as it arrived. Copy, reading
bookmarks, unread detection and the committed snapshot read the arrived text.
`ItemView` (chat, reasoning and code rows, including external-harness overlay
rows) displays the prefix that `useStreamReveal` returns. The store, the fold and
every other component are untouched.

- `reveal/controller.ts` is a pure `RevealController`: no React, no timers, the
  caller passes the time. It moves a read pointer over the arrived text by
  grapheme cluster (`Intl.Segmenter`, incremental), so an emoji, a ZWJ sequence, a
  flag or a combining mark is never cut, and a trailing lone surrogate is held
  back until its pair arrives.
- `reveal/use-stream-reveal.ts` drives it on `requestAnimationFrame` and commits
  React state at most once per `streamingParseIntervalMs` (the markdown / Prism
  parse budget), so a 60 fps reveal never means a 60 fps re-parse. The loop stops
  when nothing is left to reveal.

## Speed law

With B the unshown backlog in graphemes:

- smoothing: `v = min(SOFT_CAP, max(MIN, B / TAU))`. The backlog decays
  exponentially, which is a low-pass filter of the arrival rate: at a steady rate
  r the display settles at r, trailing the arrival by about TAU.
- lag bound: every arrival carries a deadline `arrival + MAX_LAG`; the controller
  raises `v` to whatever the earliest unmet deadline needs. No grapheme is shown
  more than MAX_LAG after it arrived, however large the burst.
- settle: when the item commits (the partial row is replaced by the committed row
  under the same id) every remaining grapheme is due `SETTLE` later. If the
  committed text no longer extends what was shown, the still-matching prefix is
  kept and the rest is revealed.

## Constants and their reasons

All values live in `reveal/constants.ts`; a change is a UI change and goes through
the preview gate.

| Constant | Value | Reason |
| --- | --- | --- |
| `REVEAL_TAU_MS` | 300 ms | Long enough to absorb the measured bursts, short enough that text still reads as live (a quarter of the lag bound). |
| `REVEAL_MIN_GRAPHEMES_PER_SEC` | 40 | Keeps text moving when the backlog is tiny (a 5-grapheme tail ends in ~125 ms instead of trailing off exponentially) and turns a one-character arrival into a step instead of a pop. |
| `REVEAL_SOFT_CAP_GRAPHEMES_PER_SEC` | 400 | A single burst is spread over about a second instead of appearing at once; ~6-7 graphemes per frame at 60 fps still reads as flow. |
| `REVEAL_MAX_LAG_MS` | 1200 ms | Upper end of the 1-1.5 s lag the product accepts as still live; the soft cap yields to this bound for very large backlogs. |
| `REVEAL_SETTLE_MS` | 250 ms | The remainder after a commit is flushed within the 300 ms product limit; nothing waits on it (Copy and the Worked line switch at the commit). |
| `REVEAL_MAX_FRAME_DT_MS` | 250 ms | After a long stall a raw time step would teleport the text; the backlog law already raised the speed during the stall. |
| `EDGE_FADE_TAIL_GRAPHEMES` | 16 | A 40-400 ms soft edge at nominal speeds: wide enough to read as a gradient, short enough never to leave stale grey text. |
| `EDGE_FADE_STEP_OPACITY` | 0.22 / 0.45 / 0.70 / 0.88 | Four steps over the tail read as a continuous ramp and need only four highlight rules. |
| `EDGE_FADE_SETTLE_MS` | 220 ms | The edge ages out instead of snapping opaque, which would be the one visible pop. |

## Which text is animated

Only text that streams in while the row is mounted. A row that mounts with text
(refresh, history, paging, a row scrolled back into the bounded window) and a
committed row whose text is replaced show it at once. A hidden tab shows
everything when it becomes visible again. Sticky-bottom follows the revealed text
because it follows the DOM that grows ([sticky scroll](sticky-scroll.md)).

## Markdown while revealing

The revealed prefix is rendered as Markdown. `repairStreamingMarkdown`
(`lib/markdown/streaming-repair.ts`) closes or hides a half-typed inline
construct at the end of the prefix for the renderer only: `**bol` renders bold
from the first character, an open inline code span is closed, `[a](http://x` shows
`a`, a half-typed image destination is dropped so no request is made for a partial
URL, and a dangling opener is hidden. An unclosed code fence needs no repair
(CommonMark renders it as a code block to the end of the input). Stored and copied
text is never altered, and the committed text renders as received.

## Leading-edge fade

While revealing, the last `EDGE_FADE_TAIL_GRAPHEMES` non-space graphemes are
painted at stepped opacity (newest faintest) with the CSS Custom Highlight API
(`reveal/edge-fade.ts`). It styles ranges of the text nodes that React and
react-markdown already own: no span per character, no layout change, and copy,
selection and accessibility see the same text. Highlight colour is derived from
the text's own colour (`color-mix(currentColor ...)`), so syntax colours in code
survive. The edge ages out over `EDGE_FADE_SETTLE_MS` when the stream settles.
A browser without the API, and a user with `prefers-reduced-motion`, get the same
smooth reveal without the gradient.

## Evidence

`reveal/recordings/real-arrivals.json` holds the delta arrival times and texts of
the provider recordings in PR 9252 (`base/lm/harness/{claude,codex,antigravity}/
tests/fixtures/streaming/*/stream.jsonl`). They are real but short (11-400
characters), so the tests run them as they are for shape and lag, and add
SYNTHETIC traces (`synthesizeArrivals`) that replay the same burst sizes and gaps
with long invented text for the variability metrics. `reveal/recordings/metrics.ts`
measures the coefficient of variation of the display speed against the arrival
speed and the worst display lag.

See [Streaming rows](streaming-rows.md).
