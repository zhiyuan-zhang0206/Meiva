from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from base.lm.harness.claude.reconciliation.judge import judge
from base.lm.harness.claude.reconciliation.transcript import reconcile_inputs, transcript_path
from base.lm.harness.delivery import Reconciliation

FIXTURE = Path(__file__).parent / "fixtures" / "session-1.jsonl"


def reconcile_input(home: Path, session: str, key: str) -> Reconciliation | None:
    answers = reconcile_inputs(home, session, [key])
    return None if answers is None else answers[key]


def home_with_transcript(tmp_path: Path) -> Path:
    target = tmp_path / "projects" / "-some-workspace" / FIXTURE.name
    target.parent.mkdir(parents=True)
    _ = shutil.copy(FIXTURE, target)
    return tmp_path


def test_a_user_row_or_a_queued_command_with_our_uuid_is_in_context() -> None:
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    assert judge(rows, "k-started") is Reconciliation.IN_CONTEXT
    assert judge(rows, "k-absorbed") is Reconciliation.IN_CONTEXT


def test_an_enqueued_but_unrecorded_input_is_not_found_and_may_be_delivered_again() -> None:
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    assert judge(rows, "k-enqueued") is Reconciliation.NOT_FOUND
    assert judge([], "k-started") is Reconciliation.NOT_FOUND


def test_the_transcript_is_found_under_the_injected_home(tmp_path: Path) -> None:
    home = home_with_transcript(tmp_path)
    assert transcript_path(home, "session-1") is not None
    assert reconcile_input(home, "session-1", "k-absorbed") is Reconciliation.IN_CONTEXT
    assert reconcile_input(home, "session-1", "k-enqueued") is Reconciliation.NOT_FOUND
    assert reconcile_input(home, "session-2", "k-absorbed") is None


def test_a_malformed_row_is_an_error_not_a_skipped_record(tmp_path: Path) -> None:
    home = home_with_transcript(tmp_path)
    path = transcript_path(home, "session-1")
    assert path is not None
    path.write_text(path.read_text() + "{not json\n")
    with pytest.raises(json.JSONDecodeError):
        _ = reconcile_input(home, "session-1", "k-absorbed")


def test_a_half_written_last_line_is_skipped_but_a_bad_line_in_the_middle_is_not(
    tmp_path: Path,
) -> None:
    home = home_with_transcript(tmp_path)
    path = transcript_path(home, "session-1")
    assert path is not None
    path.write_text(path.read_text() + '{"type": "user", "uuid": "cut off by a ki')

    assert reconcile_input(home, "session-1", "k-absorbed") is Reconciliation.IN_CONTEXT
    assert reconcile_input(home, "session-1", "k-enqueued") is Reconciliation.NOT_FOUND


def test_a_batch_is_judged_with_one_read(tmp_path: Path) -> None:
    home = home_with_transcript(tmp_path)

    assert reconcile_inputs(home, "session-1", ["k-absorbed", "k-enqueued"]) == {
        "k-absorbed": Reconciliation.IN_CONTEXT,
        "k-enqueued": Reconciliation.NOT_FOUND,
    }
