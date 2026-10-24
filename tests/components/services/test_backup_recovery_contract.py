from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).parents[3]


def _module_docstring(path: Path) -> str:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


# meiva:note
# The module note of a file: its leading `# meiva:note` comment block, without the comment marks.
def _leading_note(path: Path) -> str:
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        lines.append(line.removeprefix("#").strip())
    return "\n".join(lines).removeprefix("meiva:note").strip()


def test_conversation_recovery_sources_are_checkpoint_tables_not_events_archive() -> None:
    backup_path = _ROOT / "services" / "backup" / "dump.py"
    checkpoint_path = _ROOT / "base" / "agents" / "history" / "checkpoint.py"
    schema_path = _ROOT / "db" / "schema.sql"

    backup_source = backup_path.read_text(encoding="utf-8")
    backup_docstring = _leading_note(backup_path).lower()
    checkpoint_docstring = _module_docstring(checkpoint_path).lower()
    schema = schema_path.read_text(encoding="utf-8").lower()

    assert "checkpoint_blobs" in backup_docstring
    assert "checkpoints" in backup_docstring
    assert "checkpoint_writes" in backup_docstring
    assert "only copy of conversation history" in backup_docstring
    assert "--exclude-table" not in backup_source
    assert "rebuildable from events" not in backup_source.lower()
    assert "full message history" in checkpoint_docstring
    assert "lives in the stored checkpoint" in checkpoint_docstring
    assert "events archive (dropped" in schema
    assert "loki archive stream" in schema
