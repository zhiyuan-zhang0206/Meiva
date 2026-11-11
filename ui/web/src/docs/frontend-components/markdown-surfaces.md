# Markdown Surfaces

Text written as Markdown by a model or an author renders through the one `ChatMarkdown` surface — a lazy boundary (`components/content/markdown.tsx`) over the renderer (`components/content/markdown-renderer.tsx`; the safe HTML subset, math and anchors are [their own node](markdown-safe-content.md), safe links):

- To user: root and linked Markdown documents, with their raw HTML rendered
  through the shared allowlist and local references resolved through the
  authenticated document resource API.
- Timeline: agent replies, human and inter-agent `inbound_chat` bodies, reasoning, compact summaries, system prompts, and system markers (`memory` / `agent_memory` / `inherited_memory` and guidance notes).
- Open-question content, memory-note bodies, and task descriptions / results (task graph hover card, kanban row).

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

The renderer arrives through the same chunk seam as the code highlighter
(`components/content/lazy-chunk.ts`): the boundary reads it from
`useChatMarkdownRenderer()`, production shares one chunk per page, and a test
supplies its own chunk with a fake renderer instead of swapping the module's
import.

Verbatim monospace `pre` stays for content that is not Markdown prose: command output, system-generated `inbound_chat` bodies, attach captions, compact requests, lifecycle markers, logs, JSON and code.

Mermaid fences load a shared diagram renderer only when mounted. Strict security
and disabled automatic startup apply to every render; diagrams follow the resolved
light/dark theme. Loading and invalid or incomplete streaming diagrams show copyable
source. Completed diagrams show horizontally scrollable SVG with source-copy controls.
Inline code remains literal. Tests inject a lazy renderer through
`MermaidRendererProvider` without requiring SVG layout in happy-dom.
