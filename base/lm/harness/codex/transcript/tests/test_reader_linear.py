from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from pathlib import Path

from base.lm.harness.codex.transcript.history import PAGE_ITEMS
from base.lm.harness.codex.transcript.tests.rig import STAMP, Rig, entries_of
from base.lm.harness.codex.transcript.tests.support import as_obj, load_json, page_rows, row_item
from base.lm.harness.session.transcript.entries import AgentText

# meiva:note
# The reader's cost must not depend on how long the thread already is (the user's ruling: strictly O(n) over the
# whole conversation, and never a call that returns a whole history). A synthetic thread of `n` items is caught up
# in pages, then `ROUNDS` rounds each add two items and one usage line; for every round the requests made, the
# items read and the bytes returned are recorded. The recordings of a short thread and a thread eighty times
# longer must give the same per-round figures, and catching up must cost a fixed number of requests per page.

ROUNDS = 25
SHORT, LONG = 40, 3200


@dataclass(frozen=True, slots=True)
class Round:
    requests: int
    items: int
    rollout_bytes: int
    returned_bytes: int


def _message(index: int) -> dict[str, object]:
    row = as_obj(copy.deepcopy(page_rows(load_json("basic/items.json"))[2]))
    row_item(row)["id"] = f"msg-{index}"
    row_item(row)["text"] = f"answer {index} " + "x" * 200
    return row


def _usage(index: int) -> dict[str, object]:
    return {
        "type": "token_usage_record",
        "timestamp": STAMP,
        "ordinal": 50_000 + index,
        "payload": {
            "response_id": f"resp-{index}",
            "turn_id": "turn-1",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    }


async def _run(tmp_path: Path, length: int) -> tuple[int, list[Round]]:
    home = tmp_path / str(length)
    home.mkdir()
    rig = Rig(home)
    rig.feed([_message(i) for i in range(length)])
    caught_up = await rig.drain()
    assert len(entries_of(caught_up)) == length
    catch_up_requests = rig.reader.requests
    rounds: list[Round] = []
    for index in range(ROUNDS):
        before = (rig.reader.requests, rig.reader.items_read, rig.reader.rollout_bytes_read)
        rig.feed([_message(length + 2 * index), _message(length + 2 * index + 1)], [_usage(index)])
        batches = await rig.drain()
        assert [e.message_ref for e in entries_of(batches) if isinstance(e, AgentText)] == [
            f"msg-{length + 2 * index}",
            f"msg-{length + 2 * index + 1}",
        ]
        rounds.append(
            Round(
                rig.reader.requests - before[0],
                rig.reader.items_read - before[1],
                rig.reader.rollout_bytes_read - before[2],
                sum(batch.size for batch in batches),
            )
        )
    return catch_up_requests, rounds


async def test_each_round_costs_the_same_however_long_the_thread_is(tmp_path: Path) -> None:
    _, short = await _run(tmp_path, SHORT)
    _, long = await _run(tmp_path, LONG)
    assert {r.items for r in short} == {2} == {r.items for r in long}
    assert max(r.requests for r in long) <= 3
    assert max(r.requests for r in long) <= max(r.requests for r in short)
    assert max(r.rollout_bytes for r in long) <= max(r.rollout_bytes for r in short)
    assert max(r.returned_bytes for r in long) <= max(r.returned_bytes for r in short) + 64


async def test_catching_up_costs_a_fixed_number_of_requests_per_page(tmp_path: Path) -> None:
    short, _ = await _run(tmp_path, SHORT)
    long, _ = await _run(tmp_path, LONG)
    pages_short, pages_long = -(-SHORT // PAGE_ITEMS), -(-LONG // PAGE_ITEMS)
    assert short <= pages_short + 3
    assert long <= pages_long + 3


def test_no_call_that_returns_a_whole_history_is_made_anywhere_in_the_reader() -> None:
    folder = Path(__file__).parent.parent
    forbidden = {"thread/read", "thread/resume", "includeTurns", "thread/turns/list"}
    for source in sorted(folder.glob("*.py")):
        tree = ast.parse(source.read_text())
        found = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not forbidden & found, (source.name, forbidden & found)
