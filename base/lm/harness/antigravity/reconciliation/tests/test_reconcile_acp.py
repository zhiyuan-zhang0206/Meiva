from __future__ import annotations

import json
from pathlib import Path

from base.lm.harness.antigravity.reconciliation.queue_files import reconcile_acp_inputs
from base.lm.harness.antigravity.transcript.input_store import (
    inputs_path,
    record_sent_input,
    text_sha256,
)
from base.lm.harness.delivery import Reconciliation

SESSION = "conversation-1"


def record_of(home: Path) -> Path:
    path = home / ".gemini/antigravity-cli/brain" / SESSION / ".system_generated/logs"
    path.mkdir(parents=True)
    return path / "transcript_full.jsonl"


def write(path: Path, *texts: str) -> None:
    rows = [
        {"type": "USER_INPUT", "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>"}
        for text in texts
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def sent(path: Path, key: str, text: str, floor: int = 0) -> None:
    record_sent_input(
        inputs_path(path), key=key, min_offset=floor, text_sha256=text_sha256(text), image_count=0
    )


def test_an_input_sent_then_recorded_is_in_context_even_if_the_host_died_before_binding_it(
    tmp_path: Path,
) -> None:
    path = record_of(tmp_path)
    sent(path, "k1", "B")
    write(path, "B")
    answers = reconcile_acp_inputs(tmp_path, SESSION, ["k1"])
    assert answers == {"k1": Reconciliation.IN_CONTEXT}


def test_an_input_sent_but_not_recorded_is_not_confirmed_by_an_equal_earlier_row(
    tmp_path: Path,
) -> None:
    path = record_of(tmp_path)
    write(path, "B")
    sent(path, "k1", "B", floor=path.stat().st_size)
    answers = reconcile_acp_inputs(tmp_path, SESSION, ["k1", "k2"])
    assert answers == {"k1": Reconciliation.UNKNOWN, "k2": Reconciliation.UNKNOWN}


def test_a_row_with_a_legacy_marker_confirms_the_key_it_names(tmp_path: Path) -> None:
    path = record_of(tmp_path)
    write(path, f"<!-- meiva-input:{text_sha256('k1')} -->\nold")
    answers = reconcile_acp_inputs(tmp_path, SESSION, ["k1", "k2"])
    assert answers == {"k1": Reconciliation.IN_CONTEXT, "k2": Reconciliation.UNKNOWN}
