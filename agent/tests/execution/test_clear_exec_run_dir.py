from pathlib import Path

from agent.process_boot import clear_exec_run_dir
from base.paths import exec_run_dir


def test_clear_exec_dir_empties_the_tree_and_keeps_the_directory(tmp_path: Path) -> None:
    exec_dir = exec_run_dir(tmp_path)
    agent_dir = exec_dir / "7"
    (agent_dir / "nested").mkdir(parents=True)
    (agent_dir / "req-dead.json").write_text("{}", encoding="utf-8")
    (agent_dir / "nested" / "leftover").write_text("x", encoding="utf-8")
    (exec_dir / "stray.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("kept", encoding="utf-8")
    (exec_dir / "link").symlink_to(outside)
    (exec_dir / "dirlink").symlink_to(tmp_path)

    clear_exec_run_dir(tmp_path)

    assert exec_dir.is_dir() and not list(exec_dir.iterdir())
    assert outside.read_text(encoding="utf-8") == "kept"


def test_clear_exec_dir_tolerates_a_missing_directory(tmp_path: Path) -> None:
    clear_exec_run_dir(tmp_path / "never-created")
