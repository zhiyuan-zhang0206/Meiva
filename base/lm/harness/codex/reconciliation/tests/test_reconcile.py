from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from base.lm.harness.codex.reconciliation.judge import judge
from base.lm.harness.codex.reconciliation.rollout import reconcile_input, rollout_path
from base.lm.harness.delivery import Reconciliation

FIXTURE = Path(__file__).parent / "fixtures" / "rollout-2026-11-10T13-30-00-thread-1.jsonl"


def home_with_rollout(tmp_path: Path) -> Path:
    target = tmp_path / "sessions" / "2026" / "11" / "10" / FIXTURE.name
    target.parent.mkdir(parents=True)
    _ = shutil.copy(FIXTURE, target)
    return tmp_path


def test_a_client_id_on_a_completed_user_item_is_in_context() -> None:
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    assert judge(rows, "steer-client-id-1") is Reconciliation.IN_CONTEXT
    assert judge(rows, "never-sent") is Reconciliation.NOT_FOUND
    assert judge([], "steer-client-id-1") is Reconciliation.NOT_FOUND


def test_the_rollout_is_found_under_the_injected_home(tmp_path: Path) -> None:
    home = home_with_rollout(tmp_path)
    assert rollout_path(home, "thread-1") is not None
    assert rollout_path(home, "thread-2") is None
    assert reconcile_input(home, "thread-1", "steer-client-id-1") is Reconciliation.IN_CONTEXT
    assert reconcile_input(home, "thread-1", "other") is Reconciliation.NOT_FOUND
    assert reconcile_input(home, "thread-2", "other") is None


def test_a_malformed_row_is_an_error_not_a_skipped_record(tmp_path: Path) -> None:
    home = home_with_rollout(tmp_path)
    path = rollout_path(home, "thread-1")
    assert path is not None
    path.write_text(path.read_text() + "{not json\n")
    with pytest.raises(json.JSONDecodeError):
        _ = reconcile_input(home, "thread-1", "steer-client-id-1")
