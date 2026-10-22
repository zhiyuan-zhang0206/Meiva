from __future__ import annotations

from pathlib import Path

from agent.execution.child import DEFAULT_OOM_SCORE_ADJ, apply_oom_preference


def test_the_child_prefers_itself_for_the_oom_kill(tmp_path: Path) -> None:
    target = tmp_path / "oom_score_adj"
    assert apply_oom_preference({}, target) == DEFAULT_OOM_SCORE_ADJ
    assert target.read_text(encoding="utf-8") == str(DEFAULT_OOM_SCORE_ADJ)


def test_zero_turns_the_preference_off_and_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "oom_score_adj"
    assert apply_oom_preference({"MEIVA_EXEC_OOM_SCORE_ADJ": "0"}, target) is None
    assert not target.exists()


def test_a_score_is_clamped_to_the_kernel_range(tmp_path: Path) -> None:
    target = tmp_path / "oom_score_adj"
    assert apply_oom_preference({"MEIVA_EXEC_OOM_SCORE_ADJ": "5000"}, target) == 1000
    assert target.read_text(encoding="utf-8") == "1000"


def test_an_unreadable_value_falls_back_to_the_default(tmp_path: Path) -> None:
    target = tmp_path / "oom_score_adj"
    assert apply_oom_preference({"MEIVA_EXEC_OOM_SCORE_ADJ": "high"}, target) == (
        DEFAULT_OOM_SCORE_ADJ
    )


# meiva:note
# macOS has no /proc, so the same boot must continue without the preference rather than fail.
def test_a_host_without_oom_score_adj_is_not_an_error(tmp_path: Path) -> None:
    assert apply_oom_preference({}, tmp_path / "missing" / "oom_score_adj") is None
