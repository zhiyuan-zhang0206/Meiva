from __future__ import annotations

from services.derived.memory_indexer import chunking
from services.derived.memory_indexer.backends.base import KIND_BODY, KIND_DESC


def test_split_note_prefers_frontmatter_description() -> None:
    content = (
        "---\ntype: Memory\ndescription: Frontmatter summary\n---\n\n# Heading\n\nBody paragraph."
    )
    desc, body = chunking.split_note(content)
    assert desc == "Frontmatter summary"
    assert "# Heading" in body


def test_split_note_extracts_h1_and_first_paragraph_for_pure_markdown() -> None:
    content = (
        "# Retry Policy\n\n"
        "The service rate-limits above 3 retries, so more retries make an outage worse.\n\n"
        "## Why\n\n"
        "Detailed explanation here."
    )
    desc, body = chunking.split_note(content)
    assert (
        desc
        == "Retry Policy — The service rate-limits above 3 retries, so more retries make an outage worse."
    )
    assert body == content
    rows = chunking.chunk_rows(content)
    assert rows[0] == (KIND_DESC, 0, desc)
    assert rows[1][0] == KIND_BODY


def test_split_note_extracts_lead_when_frontmatter_has_no_description() -> None:
    content = (
        "---\nverified: '2026-11-09'\n---\n\n"
        "# Cluster Rule\n\n"
        "Always run preflight before merging.\n\n"
        "## Details\n\n"
        "More text."
    )
    desc, body = chunking.split_note(content)
    assert desc == "Cluster Rule — Always run preflight before merging."
    assert "# Cluster Rule" in body


def test_split_note_truncates_extracted_description_at_400_chars() -> None:
    content = "# Heading\n\n" + ("word " * 120) + "\n\n## Section\n\nMore."
    desc, _ = chunking.split_note(content)
    assert desc is not None
    assert len(desc) <= 400
    assert desc.startswith("Heading — word ")


def test_split_note_returns_none_for_empty_or_malformed_frontmatter() -> None:
    assert chunking.split_note("   \n\n  ") == (None, "   \n\n  ")
    assert chunking.split_note("---\ndescription: \n---\n\n   \n") == (None, "\n   \n")
    assert chunking.split_note("---\ntags: [unclosed\n---\nbody\n") == (
        None,
        "---\ntags: [unclosed\n---\nbody\n",
    )


def test_chunk_body_uses_600_max_and_100_overlap_defaults() -> None:
    p1 = "a" * 450
    p2 = "b" * 90
    p3 = "c" * 200
    chunks = chunking.chunk_body(f"{p1}\n\n{p2}\n\n{p3}")
    assert chunks == [f"{p1}\n\n{p2}", f"{p2}\n\n{p3}"]


def test_chunk_body_does_not_mix_different_markdown_sections() -> None:
    body = (
        "# Title\n\nIntro paragraph.\n\n"
        "## First Section\n\nFirst section body.\n\n"
        "## Second Section\n\nSecond section body."
    )
    chunks = chunking.chunk_body(body)
    assert chunks == [
        "# Title\n\nIntro paragraph.",
        "## First Section\n\nFirst section body.",
        "## Second Section\n\nSecond section body.",
    ]


def test_chunk_body_carries_section_heading_into_split_subchunks() -> None:
    p1 = "alpha " * 45
    p2 = "bravo " * 45
    p3 = "charlie " * 45
    body = f"## Long Section\n\n{p1.strip()}\n\n{p2.strip()}\n\n{p3.strip()}"
    chunks = chunking.chunk_body(body)
    assert len(chunks) == 2
    assert all(len(c) <= 600 for c in chunks)
    assert chunks[0].startswith("## Long Section\n\n")
    assert chunks[1].startswith("## Long Section\n\n")
    assert "alpha" in chunks[0] and "bravo" in chunks[0]
    assert "charlie" in chunks[1]
