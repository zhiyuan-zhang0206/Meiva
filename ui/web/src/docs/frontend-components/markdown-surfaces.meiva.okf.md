---
type: doc
title: Markdown Surfaces
description: Which frontend text renders through the shared ChatMarkdown surface and which stays verbatim monospace.
tags:
- frontend
---

# Markdown Surfaces

Text written as Markdown by a model or an author renders through the one `ChatMarkdown` surface — a lazy boundary (`components/content/markdown.tsx`) over the renderer (`components/content/markdown-renderer.tsx`; the safe HTML subset, math and anchors are [[ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md|their own node]], safe links):

- To user: root and linked Markdown documents, with their raw HTML rendered
  through the shared allowlist and local references resolved through the
  authenticated document resource API.
- Timeline: agent replies, reasoning (when `display.render_reasoning_markdown` is on), compact summaries, inter-agent `inbound_chat` bodies, impersonation messages, and the memory marker family (`memory` / `agent_memory` / `inherited_memory`).
- Open-notice and fleet-inbox content, memory-note bodies, and task descriptions / results (task graph hover card, kanban row).

Notice titles and question option labels/descriptions use the same renderer in
inline mode, including resolved history. Emphasis, links and inline code render
inside phrasing-only spans, keeping headings and form labels valid and retaining
their compact typography. Paragraphs flatten to inline text; block layout and
code-copy controls stay in the full content surface. Inline mode narrows the HTML
allowlist before KaTeX runs, preserving its generated SVG and accessible MathML;
both modes keep the same safe URL handling. The lazy fallback also remains inline
inside these labels.
Queue-row titles render the same formatting but keep link labels inert inside
the row-selection button; links remain active in the notice detail.

Verbatim monospace `pre` stays for content that is not Markdown prose: human messages, command output, attach captions, compact requests, the system prompt, guidance-note markers, logs, JSON and code.
