import os
import subprocess
import sys
from pathlib import Path

import pytest

from meiva_builtins.plugins.meiva_memory.pool_ops import verify_root_counts

# meiva:note
# This test pins the whole MEMORY.md file for a tiny pool. The line format has a second owner,
# `tests/test_meiva_memory_pool_index.py`; a format change must update both (2026-11-02: the
# other test went green while this one went red, one CI round trip later).
# meiva:link meiva_builtins/plugins/meiva_memory/pool_ops.py
SCRIPT = Path(__file__).parent.parent / "rebuild_memory_index.py"


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def _note(pool: Path, rel: str, text: str) -> None:
    target = pool / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_the_rebuild_writes_one_pointer_line_per_note_and_directory(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    pool.mkdir()
    (pool / "MEMORY.md").write_text(
        "Preamble text.\n\n`## Pointers` is only quoted in prose.\n\n## Pointers\n\n- [stale](stale.md) \u2014 old\n",
        encoding="utf-8",
    )
    _note(pool, "AGENTS.md", "rules\n")
    _note(pool, "index.md", "index\n")
    _note(pool, "log.md", "log\n")
    _note(pool, "alpha.md", "---\ntitle: Alpha\ndescription: first note\n---\nbody\n")
    _note(pool, "beta.md", "not a frontmatter block\n")
    _note(pool, "people/bob.md", "---\ntitle: Bob\ndescription: person\n---\n")
    _note(pool, "people/carol.md", "---\ntitle: Carol\n---\n")
    _note(pool, "people/roles/lead.md", "---\ntitle: Lead\n---\n")

    res = _run(tmp_path)

    assert res.returncode == 0, res.stderr
    assert (pool / "MEMORY.md").read_text(encoding="utf-8") == (
        "Preamble text.\n\n`## Pointers` is only quoted in prose.\n\n"
        "## Pointers\n\n\n"
        "- [Alpha](alpha.md) \u2014 first note\n"
        "- [beta](beta.md) \u2014 \n"
        "- [people/](people/index.md) \u2014 3 notes\n"
    )


def test_a_pool_without_an_index_file_gets_one(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    pool.mkdir()
    _note(pool, "solo.md", "---\ntitle: Solo\ndescription: only note\n---\n")

    res = _run(tmp_path)

    assert res.returncode == 0, res.stderr
    assert (pool / "MEMORY.md").read_text(encoding="utf-8") == (
        "## Pointers\n\n\n- [Solo](solo.md) \u2014 only note\n"
    )


def test_a_missing_heading_appends_after_the_existing_preamble(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    pool.mkdir()
    (pool / "MEMORY.md").write_text("Setup prose only.\n", encoding="utf-8")
    _note(pool, "solo.md", "---\ntitle: Solo\n---\n")

    res = _run(tmp_path)

    assert res.returncode == 0, res.stderr
    assert (pool / "MEMORY.md").read_text(encoding="utf-8") == (
        "Setup prose only.\n\n## Pointers\n\n\n- [Solo](solo.md) \u2014 \n"
    )


# meiva:note
# The root counts are the rebuild's own derived output, so it proves them itself: after writing
# `MEMORY.md` it recounts from a fresh enumeration and refuses a file that disagrees (405,
# 2026-11-06). Gating them in the pool's PR check instead would red every PR that adds a note,
# because they trail by design until the next daily rebuild.
def test_a_root_count_that_disagrees_with_the_notes_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pool = tmp_path / "memory"
    (pool / "topics").mkdir(parents=True)
    _note(pool, "topics/a.md", "---\ntitle: A\ndescription: a\n---\n")
    _note(pool, "topics/b.md", "---\ntitle: B\ndescription: b\n---\n")
    (pool / "MEMORY.md").write_text(
        "## Pointers\n\n\n- [topics/](topics/index.md) \u2014 1 notes\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit):
        verify_root_counts(pool, pool / "MEMORY.md")

    assert "topics/: says 1 notes, expected 2" in capsys.readouterr().err


def test_matching_root_counts_pass_without_an_error(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    (pool / "topics").mkdir(parents=True)
    _note(pool, "topics/a.md", "---\ntitle: A\ndescription: a\n---\n")
    _note(pool, "MEMORY.md", "## Pointers\n\n\n- [topics/](topics/index.md) \u2014 1 notes\n")

    verify_root_counts(pool, pool / "MEMORY.md")
