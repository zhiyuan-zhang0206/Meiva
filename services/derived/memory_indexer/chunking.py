# meiva:note
# Note-text chunking for the embedding index.
#
# A long note's single embedding dilutes the entities mentioned in it (queries
# like "hand off to 402" missed notes whose body carried the id). The body is
# split at Markdown section (`##`..`######`) and paragraph boundaries into
# fine-grained blocks of ~600 chars, overlapping by ~100 chars within a section
# and carrying the section heading into every sub-chunk so context is preserved.
# The frontmatter `description` (or, for pure Markdown notes, the `# H1` title
# plus the first overview paragraph before the first `##` section) is embedded as
# its own `KIND_DESC` row on top of that.

from __future__ import annotations

import re

from base.packages.docs.notes import parse_note
from services.derived.memory_indexer.backends.base import KIND_BODY, KIND_DESC

_CHUNK_MAX_CHARS = 600
_CHUNK_OVERLAP_CHARS = 100
_DESC_MAX_CHARS = 400

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SECTION_HEADING_LINE_RE = re.compile(r"^#{2,6}\s+\S.*$")
_H1_LINE_RE = re.compile(r"^#\s+(.+)$")
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _preamble_before_first_section(body: str) -> str:
    cleaned = _HTML_COMMENT_RE.sub("", body.replace("\r\n", "\n"))
    preamble_lines: list[str] = []
    in_fence = False
    for line in cleaned.splitlines():
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
        if not in_fence and _SECTION_HEADING_LINE_RE.match(line.strip()):
            break
        preamble_lines.append(line)
    return "\n".join(preamble_lines).strip()


def _parse_title_and_lead(blocks: list[str]) -> tuple[str | None, str | None]:
    first_lines = blocks[0].splitlines()
    m_h1 = _H1_LINE_RE.match(first_lines[0].strip())
    if not m_h1:
        return None, re.sub(r"\s+", " ", blocks[0])
    h1_title = m_h1.group(1).strip()
    remainder = "\n".join(first_lines[1:]).strip()
    if remainder:
        return h1_title, re.sub(r"\s+", " ", remainder)
    first_para = re.sub(r"\s+", " ", blocks[1]) if len(blocks) > 1 else None
    return h1_title, first_para


def _extract_lead_description(body: str) -> str | None:
    preamble = _preamble_before_first_section(body)
    blocks = [b.strip() for b in re.split(r"\n[ \t]*\n", preamble) if b.strip()]
    if not blocks:
        return None
    h1_title, first_para = _parse_title_and_lead(blocks)
    parts = [p for p in (h1_title, first_para) if p]
    summary = " — ".join(parts).strip()
    return summary[:_DESC_MAX_CHARS].rstrip() if summary else None


def split_note(content: str) -> tuple[str | None, str]:
    # meiva:note
    # Split one markdown file into (description, body). When the file has YAML
    # frontmatter with a non-empty `description`, returns `(description, note.body)`.
    # When the file is pure Markdown without YAML frontmatter (or its frontmatter
    # has no `description`), extracts the `# H1` title (if present) and the first
    # non-empty overview paragraph before the first `##`..`######` section heading
    # (up to 400 chars) as `description`.
    note = parse_note(content, "memory.md")
    if note is not None:
        description = note.description.strip() if note.description else None
        if description:
            return description, note.body
        return _extract_lead_description(note.body), note.body
    if content.startswith("---\n"):
        return None, content
    return _extract_lead_description(content), content


def _split_sections(body: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    in_fence = False

    for line in body.replace("\r\n", "\n").splitlines():
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
        stripped = line.strip()
        if not in_fence and _SECTION_HEADING_LINE_RE.match(stripped):
            text = "\n".join(current_lines).strip()
            if current_heading is not None or text:
                sections.append((current_heading, text))
            current_heading = stripped
            current_lines = []
        else:
            current_lines.append(line)

    tail = "\n".join(current_lines).strip()
    if current_heading is not None or tail:
        sections.append((current_heading, tail))
    return sections


def _chunk_paragraphs(
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current, current_len
        chunks.append("\n\n".join(current))
        tail: list[str] = []
        tail_len = 0
        for para in reversed(current):
            if tail_len + len(para) + (2 if tail_len else 0) > overlap_chars:
                break
            tail.insert(0, para)
            tail_len += len(para) + (2 if tail_len > 0 else 0)
        current = tail
        current_len = tail_len

    step_back = min(max(overlap_chars, 0), max(max_chars - 1, 0))
    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            start = 0
            while start < len(para):
                end = min(start + max_chars, len(para))
                chunks.append(para[start:end])
                if end == len(para):
                    break
                start = end - step_back
            continue
        if current and current_len + 2 + len(para) > max_chars:
            _flush()
        current.append(para)
        current_len += len(para) + (2 if current_len else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_body(
    body: str,
    *,
    max_chars: int = _CHUNK_MAX_CHARS,
    overlap_chars: int = _CHUNK_OVERLAP_CHARS,
) -> list[str]:
    # meiva:note
    # Split body text into fine-grained chunks along Markdown section and paragraph boundaries.
    # Sections delimited by `^#{2,6}\s+.*` never mix across boundaries. Within a
    # section, paragraphs pack greedily up to `max_chars` with `overlap_chars`
    # overlap; when a titled section spans multiple chunks, every sub-chunk
    # carries the section's heading line (`## Section Title\n\n...`) at the top.
    body = body.strip()
    if not body:
        return []

    chunks: list[str] = []
    for heading, section_text in _split_sections(body):
        if heading is None:
            chunks.extend(
                _chunk_paragraphs(section_text, max_chars=max_chars, overlap_chars=overlap_chars)
            )
            continue
        if not section_text:
            chunks.append(heading[:max_chars])
            continue
        prefix = f"{heading}\n\n"
        if len(prefix) >= max_chars:
            chunks.extend(
                _chunk_paragraphs(
                    f"{prefix}{section_text}",
                    max_chars=max_chars,
                    overlap_chars=overlap_chars,
                )
            )
            continue
        inner_max = max_chars - len(prefix)
        inner_overlap = min(overlap_chars, max(inner_max - 1, 0))
        sub_chunks = _chunk_paragraphs(
            section_text,
            max_chars=inner_max,
            overlap_chars=inner_overlap,
        )
        chunks.extend(f"{prefix}{sub}" for sub in sub_chunks)
    return chunks


def chunk_rows(content: str) -> list[tuple[str, int, str]]:
    # meiva:note
    # The chunk rows of one file: (kind, chunk_idx, text). The frontmatter
    # `description` becomes one KIND_DESC row when present; the body splits into
    # KIND_BODY chunks. A file with neither produces no rows (it is not searchable
    # content; the cold-start reconcile tolerates that).

    description, body = split_note(content)
    rows: list[tuple[str, int, str]] = []
    if description:
        rows.append((KIND_DESC, 0, description))
    for i, chunk in enumerate(chunk_body(body)):
        rows.append((KIND_BODY, i, chunk))
    return rows


def chunk_text(content: str, kind: str, chunk_idx: int) -> str | None:
    """The text of one indexed row of a file, restored from its current content.

    `kind` and `chunk_idx` are the coordinates a search hit carries. Returns None
    when the file no longer produces that row (it was edited since it was
    indexed), so a caller never pairs a stale row with unrelated text.
    """
    for row_kind, row_idx, text in chunk_rows(content):
        if row_kind == kind and row_idx == chunk_idx:
            return text
    return None
