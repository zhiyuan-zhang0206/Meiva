from __future__ import annotations

from pathlib import Path

from meiva.tests.namespaces.understand.provider_support import UnderstandHarness
from meiva.tests.namespaces.understand.provider_support import (
    understand_harness as understand_harness,
)


def test_single_result_saved_to_exec_output(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    exec_dir = tmp_path / "fake_ws" / ".exec_output"
    understand_harness.save_to(exec_dir)

    understand_harness.understand([{"prompt": "summarize please", "text": "hello world"}])

    [saved] = exec_dir.glob("understand_*.txt")
    content = saved.read_text()
    assert "fake answer" in content
    assert "summarize please" in content


def test_batch_results_saved_to_exec_output_in_submission_order(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    exec_dir = tmp_path / "batch_ws" / ".exec_output"
    understand_harness.save_to(exec_dir)

    understand_harness.understand(
        [
            {"prompt": "question one", "text": "material one"},
            {"prompt": "question two", "text": "material two"},
        ]
    )

    first, second = sorted(exec_dir.glob("understand_*.txt"))
    assert "question one" in first.read_text()
    assert "question two" in second.read_text()


def test_auto_save_prunes_old_files(understand_harness: UnderstandHarness, tmp_path: Path) -> None:
    exec_dir = tmp_path / "prune_ws" / ".exec_output"
    exec_dir.mkdir(parents=True)
    for i in range(30):
        (exec_dir / f"understand_20240101_000000_{i:06d}_old{i}.txt").write_text(f"old result {i}")
    understand_harness.save_to(exec_dir)

    understand_harness.understand([{"prompt": "new question", "text": "new material"}])

    names = sorted(f.name for f in exec_dir.glob("understand_*.txt"))
    assert len(names) == 20, "the ring retains the newest 20 results"
    assert [n for n in names if "_old" in n] == [
        f"understand_20240101_000000_{i:06d}_old{i}.txt" for i in range(11, 30)
    ]
    assert sum(1 for n in names if "_old" not in n) == 1, "the newly written file survives"


def test_nothing_is_saved_outside_an_agent_process(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    assert understand_harness.deps.output is None

    [result] = understand_harness.understand([{"prompt": "test", "text": "some text"}])

    assert result == "fake answer"
    assert list(tmp_path.glob("**/understand_*.txt")) == []


def test_one_question_is_a_one_element_batch(understand_harness: UnderstandHarness) -> None:
    [result] = understand_harness.understand([{"prompt": "hello", "text": "world"}])
    assert result == "fake answer"
