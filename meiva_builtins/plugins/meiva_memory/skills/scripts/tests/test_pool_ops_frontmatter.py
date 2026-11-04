from __future__ import annotations

from pathlib import Path

import pytest

from meiva_builtins.plugins.meiva_memory.pool_ops import frontmatter


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "---\ntitle: A note\ntags: [type/user]\n---\nbody\n",
            {"title": "A note", "tags": ["type/user"]},
        ),
        ("no frontmatter at all\n", {}),
        ("---\n---\nbody\n", {}),
    ],
    ids=["mapping", "absent", "empty"],
)
def test_frontmatter_is_the_mapping_or_empty(
    tmp_path: Path, text: str, expected: dict[str, object]
) -> None:
    _ = (tmp_path / "note.md").write_text(text, encoding="utf-8")

    assert frontmatter(tmp_path, "note.md") == expected


def test_a_frontmatter_that_is_not_a_mapping_is_refused_by_name(tmp_path: Path) -> None:
    _ = (tmp_path / "note.md").write_text("---\n- just\n- a list\n---\nbody\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"note\.md: frontmatter must be a mapping, got list"):
        _ = frontmatter(tmp_path, "note.md")


def test_a_frontmatter_that_is_not_valid_yaml_is_refused_by_name(tmp_path: Path) -> None:
    _ = (tmp_path / "note.md").write_text("---\n: : not [valid\n---\nbody\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"note\.md: frontmatter is not valid YAML"):
        _ = frontmatter(tmp_path, "note.md")
