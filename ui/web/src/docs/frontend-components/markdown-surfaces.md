# Markdown Surfaces

Text written as Markdown by a model or an author renders through the one `ChatMarkdown` surface — a lazy boundary (`components/content/markdown.tsx`) over the renderer (`components/content/markdown-renderer.tsx`; the safe HTML subset, math and anchors are [their own node](markdown-safe-content.md), safe links):

- To user: root and linked Markdown documents, with their raw HTML rendered
  through the shared allowlist and local references resolved through the
  authenticated document resource API.
- Timeline: agent replies, human and inter-agent `inbound_chat` bodies, reasoning, compact summaries, system prompts, and system markers (`memory` / `agent_memory` / `inherited_memory` and guidance notes).
- Open-question content, memory-note bodies, and task descriptions / results (task graph hover card, kanban row).

Ordinary Markdown images use the authenticated workspace byte reader for local
references. Absolute `.meiva/workspaces/<id>/...` references name the file's
owner, which can differ from the agent whose conversation shows the image.
Relative/cwd references use the selected agent, matching file-link navigation.
The gateway retains placement and workspace/cwd access checks; image rendering
does not grant access to arbitrary paths on the frontend or gateway machine.
Remote and upload images keep their URLs. The To user panel's explicit document
resolver remains authoritative for its registered resources.

Question titles and question option labels/descriptions use the same renderer in
inline mode, including resolved history. Emphasis, links and inline code render
inside phrasing-only spans, keeping headings and form labels valid and retaining
their compact typography. Paragraphs flatten to inline text; block layout and
code-copy controls stay in the full content surface. Inline mode narrows the HTML
allowlist before KaTeX runs, preserving its generated SVG and accessible MathML;
both modes keep the same safe URL handling. The lazy fallback also remains inline
inside these labels.
Queue-row titles render the same formatting but keep link labels inert inside
the row-selection button; links remain active in the question detail.

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

Production diagrams use the trusted response nonce for both measurement and the
returned SVG's generated root stylesheet. Mermaid 12.3.0 has no nonce option, so
its pinned pnpm patch reads the caller-owned measurement host's nonce before
inserting that stylesheet. The renderer reapplies the nonce to the sanitized
root stylesheet because serialization hides it on connected elements. Labels
and other source-authored elements never receive the response nonce. Keep the
patch until an upstream nonce API covers measurement as well as returned SVG;
a dependency update must verify both under the production policy. The library's
internal sanitizer can still report a transient style rejection while parsing
its serialized stylesheet; final diagram styles are authorized. Strict security
and the production CSP remain required.
