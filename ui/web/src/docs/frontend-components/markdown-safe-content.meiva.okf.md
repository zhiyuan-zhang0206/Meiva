---
type: doc
title: Markdown safe content
description: The ChatMarkdown renderer's sanitized HTML subset, lazy KaTeX math and document anchors — allowlist, URL resolution order, id handling and the tests that pin them.
tags:
- frontend
---

# Markdown safe content

The renderer's content extensions, split out of
[[ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md]]: what raw
HTML may render, how math arrives, and how in-document anchors line up.

## The safe HTML subset

Allowed raw HTML renders; a tag outside the allowlist unwraps to its children,
and `script`, `style`, `iframe` and `template` are stripped with their content.
`style` and event-handler attributes never survive. URLs resolve before the
sanitizer judges them, so a scheme the allowlist does not name can still become
its final authenticated URL; anything unresolved falls back to `about:blank`,
which the sanitizer then drops — an unsafe URL never renders as a live link.
react-markdown repeats the resolver after the sanitizer (its own `post()` pass),
so the resolver must return an already-resolved absolute `http(s)` URL
unchanged — a relative result would be rewritten to `about:blank` by that
second pass.
Ids render as authored: GFM footnote ids and rehype-slug headings author their
own, and the sanitizer's clobber prefix would desynchronize every id/anchor
pair, so it is disabled (scripts and styles are stripped, and nothing resolves
nodes through named globals).

Raw HTML renders through the GitHub-style sanitize allowlist extended with figure/figcaption/mark; script, style, iframe and template are stripped with their content, style and event-handler attributes never survive, and other non-allowlisted tags unwrap to their children. The resolver rewrites every a[href] / img[src] to its final URL before sanitizing, so file: and other local schemes reach the protocol check resolved; an unresolved value becomes about:blank, which the sanitizer drops, so an unsafe URL never renders as a live link. Ids render as authored — GFM footnote ids and rehype-slug headings author their own and the clobber prefix is disabled — while scripts and styles are stripped and nothing resolves nodes through named globals.

## Math

Inline `$x$` / `\(x\)` and display `$$…$$` / `\[…\]` math render through KaTeX,
loaded lazily: content without `$`, `\(`, `\[` or a `math` code fence never
imports the math chunk, and KaTeX output renders after the sanitizer — its
inputs cannot produce injection and its trust and strict defaults stand.

The chunk is a value, like the code chunk: `lib/markdown/math.ts` builds a
source with one deferred import per instance (the first attempt shared, a failed
attempt dropped so the next caller retries), `MarkdownMathProvider` supplies it,
and production wires nothing — the module's own source is the provider's
default, so every surface shares one cache. A test wraps its tree in the
provider with a source built around a fake importer, which is why no shipped
module exports a test-only reset or import override for math.

`remark-math` runs with `singleDollarTextMath: false` so it still owns `$$…$$`
and `mdast-util-math`, while `lib/markdown/remark-latex-math.ts` tokenizes
single-dollar `$…$` and explicit `\(...\)` / `\[...\]` delimiters inside
`micromark` before emphasis, inline code, autolinks or CommonMark character
escapes run. Single-dollar math follows Pandoc's `tex_math_dollars` boundaries
(not preceded by an unescaped `$` or ASCII alphanumeric, no whitespace just
inside either `$`, no digit immediately after the closing `$`, single-line
only) and rejects candidate spans carrying backticks, CJK or typography
punctuation outside `\text{…}`-family groups, unbraced `**` emphasis runs, or
currency-leading prose (`$30 for …`, `($30) and …`). Rolling back at tokenize
time keeps currency amounts (`**$0.51 / $40.00**`, `**$30 …**, $40`) and
surrounding emphasis intact instead of swallowing delimiters into a dead math
token. Mid-line `\[1\]` and `\[TODO\]` stay CommonMark bracket escapes unless
padded with spaces or carrying math syntax; standalone or multi-line `\[…\]`
always renders as display math.

Display math needs help from two places, because `remark-math` treats a
single-line `$$…$$` pair as *inline* math (only a fence on its own line, content
below, is flow math), and single-paragraph `\[…\]` spans share the same inline
node. A recovery plugin reads `$$…$$` and `\[…\]` delimiters back from the
source and promotes such a span to a `math` block, splitting the paragraph
around it; the promoted node carries the `data` shape `mdast-util-math` uses,
because `mdast-util-to-hast` has no handler for `math` and a bare node renders as
its raw TeX text. The sanitizer keeps `language-math` on the `code` element so
`rehype-katex` can replace the wrapping `pre` — without it a display formula
keeps code-block chrome. A display formula wider than the column scrolls
sideways (`.katex-display` overflow), and `.chat-md .katex` resets the
container's `overflow-wrap:anywhere`, which otherwise breaks a formula
mid-expression.

## Document anchors

Headings gain stable slug ids — deduplicated — and same-document links target
them; GFM footnotes keep every anchor pair matching after sanitizing; the To
user panel keeps page-anchor links in the panel.
