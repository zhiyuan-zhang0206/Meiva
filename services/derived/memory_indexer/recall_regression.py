# meiva:note
# Passive recall's regression set: 30 real inbound turns on which a note the agent needed was
# retrieved but scored below the injection threshold, each with the rank and score it held on
# 2026-10-18. A change to the ranking question, the model, the candidate count or the chunker
# moves these numbers; this module turns a fresh run into held/slipped/lost so the change is
# judged by what recall did, not by what the code says it should do.
# meiva:link services/docs/gateway_side/memory_indexer/memory-recall-regression.meiva.okf.md

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

CASES_PATH = Path(__file__).resolve().parent / "tests" / "recall_cases.jsonl"
DEFAULT_TOP = 5
DEFAULT_THRESHOLD = 0.72


@dataclass(frozen=True)
class Case:
    id: str
    gloss: str
    language: str
    shape: str
    message: str
    path: str
    baseline_rank: int
    baseline_score: float


@dataclass(frozen=True)
class Outcome:
    case: Case
    rank: int | None
    score: float | None

    @property
    def verdict(self) -> str:
        if self.rank is None:
            return "lost"
        return "held" if self.rank <= self.case.baseline_rank else "slipped"

    def delivered(self, threshold: float) -> bool:
        return (
            self.rank is not None
            and self.rank <= 3
            and self.score is not None
            and self.score >= threshold
        )


def _text(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    return float(value)


def _case_of(raw: object) -> Case:
    if not isinstance(raw, dict):
        raise TypeError("each case must be a JSON object")
    row = cast(dict[str, object], raw)
    expect = row["expect"]
    if not isinstance(expect, dict):
        raise TypeError(f"case {row.get('id')!r}: expect must be an object")
    match = cast(dict[str, object], expect)
    return Case(
        id=_text(row["id"]),
        gloss=_text(row["gloss"]),
        language=_text(row.get("language", "")),
        shape=_text(row.get("shape", "")),
        message=_text(row["message"]),
        path=_text(match["path"]),
        baseline_rank=int(_number(match["baseline_rank"])),
        baseline_score=_number(match["baseline_score"]),
    )


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    lines = path.read_text(encoding="utf-8").splitlines()
    cases = [_case_of(json.loads(line)) for line in lines if line.strip()]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id!r}")
        seen.add(case.id)
        if not case.message.strip():
            raise ValueError(f"case {case.id!r}: empty message")
        if case.baseline_rank < 1:
            raise ValueError(f"case {case.id!r}: baseline_rank must be >= 1")
    return cases


def evaluate(case: Case, ranked: Sequence[tuple[str, float]]) -> Outcome:
    for position, (path, score) in enumerate(ranked, 1):
        if path == case.path:
            return Outcome(case=case, rank=position, score=float(score))
    return Outcome(case=case, rank=None, score=None)


def report(outcomes: Iterable[Outcome], *, threshold: float = DEFAULT_THRESHOLD) -> str:
    rows = list(outcomes)
    held = [o for o in rows if o.verdict == "held"]
    slipped = [o for o in rows if o.verdict == "slipped"]
    lost = [o for o in rows if o.verdict == "lost"]
    delivered = [o for o in rows if o.delivered(threshold)]
    lines = [
        f"recall regression: {len(rows)} cases, threshold {threshold:.2f}",
        f"  delivered {len(delivered):>3}  (top three, at or above the threshold)",
        f"  held      {len(held):>3}",
        f"  slipped   {len(slipped):>3}",
        f"  lost      {len(lost):>3}",
    ]
    for label, group in (("slipped", slipped), ("lost", lost)):
        for outcome in group:
            if outcome.rank is None:
                lines.append(
                    f"    {label} {outcome.case.id}: {outcome.case.path} no longer returned"
                )
            else:
                lines.append(
                    f"    {label} {outcome.case.id}: {outcome.case.path} "
                    f"rank {outcome.case.baseline_rank} -> {outcome.rank}, "
                    f"score {outcome.case.baseline_score:.2f} -> {outcome.score:.2f}"
                )
    return "\n".join(lines)


def _ranked_from(entry: object) -> list[tuple[str, float]]:
    if not isinstance(entry, list):
        raise TypeError("each results entry must be a list of {path, score} objects")
    ranked: list[tuple[str, float]] = []
    for item in cast(list[object], entry):
        if not isinstance(item, dict):
            raise TypeError("each match must be an object with path and score")
        match = cast(dict[str, object], item)
        ranked.append((_text(match["path"]), _number(match["score"])))
    return ranked


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a fresh passive-recall run against the recorded 2026-10-18 baseline. "
            "The results file maps a case id to its ranked matches, best first: "
            '{"recall-01": [{"path": "a/b.md", "score": 0.74}, ...]}'
        )
    )
    parser.add_argument(
        "--results", required=True, type=Path, help="JSON: case id -> ranked matches"
    )
    parser.add_argument(
        "--cases", type=Path, default=CASES_PATH, help="fixture to evaluate against"
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="injection threshold in force"
    )
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP, help="matches per case the run compared"
    )
    args = parser.parse_args(argv)

    decoded: object = json.loads(args.results.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise SystemExit("results must be a JSON object mapping case id to ranked matches")
    payload = cast(dict[str, object], decoded)
    cases = load_cases(args.cases)
    missing = [case.id for case in cases if case.id not in payload]
    if missing:
        raise SystemExit(f"results are missing {len(missing)} case(s), e.g. {missing[0]}")
    outcomes = [evaluate(case, _ranked_from(payload[case.id])[: args.top]) for case in cases]
    sys.stdout.write(report(outcomes, threshold=args.threshold) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
