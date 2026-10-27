from pathlib import Path
from typing import Any

from base.agents.context import MeivaContext
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.paths import memory_dir
from base.paths.home import resolve_meiva_home
from meiva_builtins.plugins.meiva_memory.notes import memory_index_note


def _context(**pins: Any) -> MeivaContext:
    return MeivaContext(
        agent=AgentSlices.resolve(pins, default_reader=defaults()),
        clock_factory=Clock.from_settings,
        meiva_home=resolve_meiva_home(),
    )


def _pool() -> Path:
    pool = memory_dir()
    pool.mkdir(parents=True)
    return pool


def test_memory_index_note_present(unit_home: Path) -> None:
    (_pool() / "MEMORY.md").write_text(
        "prod=~/.meiva/source\n- people -> people/", encoding="utf-8"
    )

    note = memory_index_note(_context(memory_index_inject_enabled=True))
    assert note is not None
    assert note.additional_kwargs["meiva_msg_type"] == "system_note"
    assert note.additional_kwargs["meiva_note_tag"] == "memory"
    assert "prod=~/.meiva/source" in note.content


def test_memory_index_note_none_when_absent_empty_or_disabled(unit_home: Path) -> None:
    pool = _pool()
    enabled = _context(memory_index_inject_enabled=True)
    assert memory_index_note(enabled) is None
    (pool / "MEMORY.md").write_text("   \n\t\n", encoding="utf-8")
    assert memory_index_note(enabled) is None
    (pool / "MEMORY.md").write_text("real content", encoding="utf-8")
    assert memory_index_note(_context(memory_index_inject_enabled=False)) is None
