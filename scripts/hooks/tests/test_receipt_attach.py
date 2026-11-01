# meiva:note
# What the attach tool promises: the block it owns is replaced rather than stacked, a receipt the
# pinned checker refuses never reaches a body, merging is a separate decision from writing, and the
# line it writes is one the real checker accepts -- the last one is the promise that matters, because
# the auditor will read the body back with the same checker.
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from scripts.hooks.diff_checks_receipt import findings, parse
from scripts.hooks.receipt_attach import (
    MARK_BEGIN,
    Pull,
    attach_if_valid,
    attached,
    may_merge,
    note_for,
    outside_receipts,
)

CONVENTION = Path(__file__).resolve().parents[3] / (
    "docs/conventions/engineering/failure-output-carries-the-why.md"
)
FIXED_HEAD = "2947c6d53230e3c594664843f814d566af291af3"

# meiva:note
# Two checkers that differ the way real ones do: the newer vocabulary requires `scope=`. Both take
# the arguments the real checker takes, so the call path under test is the real one.
_HEADER = (
    "import argparse, sys\n"
    "parser = argparse.ArgumentParser()\n"
    "parser.add_argument('--receipt-file')\n"
    "parser.add_argument('--body-file')\n"
    "parser.add_argument('--head')\n"
    "parser.add_argument('--repo-root')\n"
    "arguments = parser.parse_args()\n"
    "line = open(arguments.receipt_file).read()\n"
)
_ACCEPTS = _HEADER + "sys.exit(0 if 'diff-checks/v1 ' in line else 1)\n"
_REFUSES = _HEADER + "sys.exit(1)\n"


def _convention_line() -> str:
    for line in CONVENTION.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff-checks/v1 "):
            return re.sub(r"@\S+", f"@{FIXED_HEAD}", line, count=1)
    raise AssertionError(f"no receipt example in {CONVENTION}")


# meiva:note
# A second copy of one fact is the failure this replaces rather than repeats: the same receipt
# attached twice leaves one block, and the note of the later attachment is the one that survives.
def test_attaching_twice_keeps_one_block() -> None:
    first = attached("what changed", "diff-checks/v1 @a base=b", "(pre-merge receipt, posted t1)")
    second = attached(first, "diff-checks/v1 @a base=b", "(after-the-fact: merged m, receipt t2)")

    assert second.count(MARK_BEGIN) == 1
    assert len(outside_receipts(second)) == 0
    assert second.count("diff-checks/v1 ") == 1
    assert "t2" in second and "t1" not in second


# meiva:note
# A body someone else wrote a receipt into by hand keeps it: this tool owns only its own block.
def test_a_hand_written_receipt_is_left_alone() -> None:
    body = "## What\n\ndiff-checks/v1 @a base=b scope=docs\n"

    assert outside_receipts(body) == ["diff-checks/v1 @a base=b scope=docs"]
    assert outside_receipts(attached(body, "diff-checks/v1 @c", "note")) == [
        "diff-checks/v1 @a base=b scope=docs"
    ]


# meiva:note
# A body that describes the block in prose is not a body that carries one: the markers count only on
# a line of their own, so a mention in backticks is left where the author wrote it.
def test_a_prose_mention_of_the_marker_is_not_a_block() -> None:
    body = "Writes into a block marked `<!-- receipt-attach:begin -->` / `<!-- receipt-attach:end -->`.\n"

    assert outside_receipts(body) == []
    assert attached(body, "diff-checks/v1 @c", "note").startswith(body.rstrip())


# meiva:note
# The red-as-green failure: a line the pinned checker refuses produces no body at all, so the caller
# has nothing to write and the pull request keeps the body it had.
def test_a_refused_receipt_never_produces_a_body(tmp_path: Path) -> None:
    body = "what changed"

    accepted, _ = attach_if_valid(_ACCEPTS, "diff-checks/v1 @a", FIXED_HEAD, tmp_path, body, "n")
    refused, detail = attach_if_valid(
        _REFUSES, "diff-checks/v1 @a", FIXED_HEAD, tmp_path, body, "n"
    )

    assert accepted is not None and accepted.startswith(body)
    assert refused is None
    assert detail


# meiva:note
# Writing and merging are separate decisions: evidence that did not verify, or a pull request that
# already merged, each stop the merge on its own.
def test_merging_needs_verified_evidence_and_an_open_pull_request() -> None:
    open_pull = Pull(8076, FIXED_HEAD, "body", None)
    merged_pull = Pull(8076, FIXED_HEAD, "body", "2026-11-01T11:02:49Z")

    assert may_merge(open_pull, verified=True)
    assert not may_merge(open_pull, verified=False)
    assert not may_merge(merged_pull, verified=True)


# meiva:note
# The note under the line says which fact it is. A receipt attached after the merge is a record of
# what was true -- not a claim that the gate ran before the merge -- and it says so.
def test_the_note_names_which_fact_the_line_is() -> None:
    now = datetime(2026, 11, 1, 19, 20, tzinfo=UTC)

    assert note_for(Pull(1, FIXED_HEAD, "", None), now=now).startswith("(pre-merge receipt")
    after = note_for(Pull(1, FIXED_HEAD, "", "2026-11-01T11:02:49Z"), now=now)
    assert after.startswith("(after-the-fact: merged 2026-11-01")
    assert "receipt posted 2026-11-02T03:20:00+08:00" in after
    assert "merged 2026-11-01 19:02:49 +0800" in after


# meiva:note
# The end-to-end promise, without a network: the body this tool writes carries a line the real
# checker accepts, so the audit that reads it back later reaches `passed` rather than `rejected`.
def test_the_block_this_tool_writes_passes_the_real_checker() -> None:
    line = _convention_line()
    body = attached(
        "what changed",
        line,
        note_for(Pull(8076, FIXED_HEAD, "", "2026-11-01T11:02:49Z"), now=datetime.now(UTC)),
    )

    assert findings(parse(line), head=FIXED_HEAD, body=body) == []
