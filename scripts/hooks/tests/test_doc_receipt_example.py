# meiva:note
# The convention shows one receipt a reader copies. A field the vocabulary renamed (the dead
# `vitest` became `vitest-app`) leaves that example failing the checker it documents, and nobody
# reads line 220 of a document to notice: the example is read by copying it into a pull request,
# where the refusal arrives too late to tell the reader it was never their fault.
#
# The check is deliberately shape-only: whatever the example says, the checker must find nothing
# wrong with it. A reason is not graded here either.
from __future__ import annotations

import re
from pathlib import Path

from scripts.hooks.diff_checks_receipt import findings, parse

CONVENTION = Path(__file__).resolve().parents[3] / (
    "docs/conventions/engineering/failure-output-carries-the-why.md"
)

# meiva:note
# The example's head is whatever commit it was measured on; the check pins it to a fixed sha so the
# assertion is about the fields, not about a commit that ages.
FIXED_HEAD = "0" * 40


# meiva:note
# The first fenced line of the convention that starts a receipt: the one a reader copies.
def _example_line() -> str:
    for line in CONVENTION.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff-checks/v1 "):
            return line
    raise AssertionError(f"no receipt example in {CONVENTION}")


def test_the_receipt_the_convention_shows_passes_the_checker_it_documents() -> None:
    line = _example_line()
    pinned = re.sub(r"@\S+", f"@{FIXED_HEAD}", line, count=1)
    problems = findings(parse(pinned), head=FIXED_HEAD, body="")
    assert problems == [], (
        "the receipt example in the convention must survive the checker it documents: a reader "
        "copies it into a pull request, and a field the vocabulary renamed leaves them holding a "
        f"line that is refused for a reason they did not cause. {problems}"
    )
