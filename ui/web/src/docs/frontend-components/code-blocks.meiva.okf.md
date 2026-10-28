---
type: doc
title: Code blocks
description: PythonCode highlighter bundle strategy — lazy import out of the home route, proactive warm-up and prefetch triggers, and when tokenization runs.
tags: [frontend]
---

# Code blocks

`PythonCode` keeps its highlighter (`prism-react-renderer`, ~85KB) out of
the `/` route's initial bundle via a plain dynamic `import()`, but warms
that chunk proactively: `CodeHighlighterPreloader` mounts on every page
with code blocks (home timeline, `/control/schedules`) and fetches it once
idle; a code block's own collapsed toggle (`CardHeader`, the schedules row
expand button) also fetches it on pointer-enter/focus, ahead of the click
that expands it. Not yet resolved → plain unhighlighted text (the surface's
copy control, when it offers one, and the streaming cursor stay present — no
blank body); already prefetched → the first render is already highlighted, no
flash. Tokenization still only runs when a code block renders or its source
changes. `copy` is a caller decision: a code surface that shows the agent's
machinery rather than text to reuse turns the corner control off — the
timeline's tool-call block passes `copy={false}`, the schedules page keeps it
(user 2026-10-29, [[ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md|card chrome]]).

The chunk itself is a value, not module state: `components/content/lazy-chunk.ts`
holds one deferred module — the cached value, one import attempt shared by every
caller, and a failed attempt dropped so the next call retries — and `PythonCode`
reads it from `usePythonCodeHighlighter()`. Production wires nothing: the
module's own chunk is the provider's default, so the whole page shares one
cache. A test wraps its tree in `PythonCodeHighlighterProvider` with a chunk
built from a fake `importModule`, which is what replaced the old
`__…ForTests` setters: no production module exports a test-only reset or
import override.

A test hook is not the way to reach that seam: what may be imported from a
shipped module at all is [[ui/web/src/docs/frontend-components/test-only-imports.meiva.okf.md|Test-only modules]].

Parent: [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md]].
