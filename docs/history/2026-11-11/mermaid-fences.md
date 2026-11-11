# Mermaid fenced diagrams

Mermaid is loaded through the existing shared lazy-chunk seam only when a
fenced diagram mounts. A renderer provider lets unit tests exercise Markdown
routing and asynchronous rendering without emulating SVG layout.

Rendering is serialized because Mermaid initialization changes global config.
Each render uses a temporary host removed in a finally block. Pending and
rejected renders retain copyable source, supporting partial streaming diagrams.
