from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.tests.support import start
from base.lm.harness.antigravity.transcript.input_store import (
    load_ledger,
    text_sha256,
    unbound_inputs,
)
from base.lm.harness.delivery import HarnessInput, ImageBlock, TextBlock
from base.lm.harness.events import InputRejected, TurnCompleted

LOGS = ".gemini/antigravity-cli/brain/conv-acp/.system_generated/logs"


async def test_an_input_is_ledgered_with_the_digest_of_its_joined_text_and_then_bound(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        sent = HarnessInput(
            "one", (TextBlock("look"), ImageBlock("image/png", "YQ=="), TextBlock("tail"))
        )
        await harness.session.deliver([sent])
        _ = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        [prompt] = harness.prompts()
        assert "meiva-input" not in str(prompt)
        ledger = load_ledger(harness.home / LOGS / "meiva-inputs.jsonl")
        assert [(s.key, s.text_sha256, s.image_count) for s in ledger.sent] == [
            ("one", text_sha256("looktail"), 1)
        ]
        assert ledger.bound == {0: "one"}
    finally:
        await harness.stop()


async def test_a_rejected_input_with_no_row_is_dropped_from_the_ledger_and_frees_its_place(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, extra_args=("--test-child",), cancel_seconds=0.2)
    try:
        await harness.session.deliver([HarnessInput.of("lost", "unwritten")])
        _ = await harness.wait_for(lambda e: isinstance(e, InputRejected) and e.key == "lost")
        ledger = load_ledger(harness.home / LOGS / "meiva-inputs.jsonl")
        assert [s.key for s in ledger.sent] == ["lost"] and ledger.dropped == ("lost",)
        assert unbound_inputs(ledger) == []
    finally:
        await harness.stop()
