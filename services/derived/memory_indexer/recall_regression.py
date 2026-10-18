# meiva:note
# Passive recall fails in two directions, so its regression set has two halves: 30 real inbound
# turns where a note the agent needed was retrieved but never delivered, and 25 where nothing in
# the pool applied yet the ranking was confident. A lower injection threshold should move the
# first half up and the second half down; a change that moves both the same way has not been
# judged. The precision half carries scores on both sides of every step it might be asked to take,
# so the next cut down cannot report "recall up, precision unchanged" by arithmetic alone.
# The ranking threshold and cap are read from the agent config, never copied here.
# meiva:link services/docs/gateway_side/memory_indexer/memory-recall-regression.meiva.okf.md

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from base.config import settings

CASES_PATH = Path(__file__).resolve().parent / "tests" / "recall_cases.jsonl"
RECALL = "recall"
PRECISION = "precision"


@dataclass(frozen=True)
class Case:
    id: str
    group: str
    gloss: str
    language: str
    shape: str
    message: str
    path: str | None
    baseline_rank: int | None
    baseline_score: float
    baseline_delivered: bool


@dataclass(frozen=True)
class Outcome:
    case: Case
    rank: int | None
    score: float | None
    threshold: float
    cap: int

    @property
    def delivered(self) -> bool:
        return (
            self.rank is not None
            and self.rank <= self.cap
            and (self.score or 0.0) >= self.threshold
        )

    @property
    def verdict(self) -> str:
        if self.case.group == PRECISION:
            return "fired" if self.delivered else "silent"
        if self.rank is None:
            return "lost"
        return "held" if self.rank <= (self.case.baseline_rank or 0) else "slipped"


def injection_policy() -> tuple[float, int]:
    memory = settings.agent
    return memory.memory_recall_inject_threshold, memory.memory_recall_inject_max


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
    path = match.get("path")
    rank = match.get("baseline_rank")
    return Case(
        id=_text(row["id"]),
        group=_text(row["group"]),
        gloss=_text(row["gloss"]),
        language=_text(row.get("language", "")),
        shape=_text(row.get("shape", "")),
        message=_text(row["message"]),
        path=None if path is None else _text(path),
        baseline_rank=None if rank is None else int(_number(rank)),
        baseline_score=_number(match["baseline_score"]),
        baseline_delivered=bool(match["baseline_delivered"]),
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
        if case.group not in (RECALL, PRECISION):
            raise ValueError(f"case {case.id!r}: unknown group {case.group!r}")
        if case.group == RECALL:
            if case.path is None or case.baseline_rank is None:
                raise ValueError(f"case {case.id!r}: a recall case needs path and baseline_rank")
            if case.baseline_rank < 1:
                raise ValueError(f"case {case.id!r}: baseline_rank must be >= 1")
        elif case.path is not None:
            raise ValueError(f"case {case.id!r}: a precision case expects no path")
    return cases


def evaluate(
    case: Case, ranked: Sequence[tuple[str, float]], *, threshold: float, cap: int
) -> Outcome:
    for position, (path, score) in enumerate(ranked, 1):
        if case.path is not None and path == case.path:
            return Outcome(
                case=case, rank=position, score=float(score), threshold=threshold, cap=cap
            )
    return Outcome(case=case, rank=None, score=None, threshold=threshold, cap=cap)


def evaluate_precision(
    case: Case, ranked: Sequence[tuple[str, float]], *, threshold: float, cap: int
) -> Outcome:
    for position, (_, score) in enumerate(list(ranked)[: max(cap, 0)], 1):
        if score >= threshold:
            return Outcome(
                case=case, rank=position, score=float(score), threshold=threshold, cap=cap
            )
    return Outcome(case=case, rank=None, score=None, threshold=threshold, cap=cap)


def _with_verdict(rows: Iterable[Outcome], verdict: str) -> list[Outcome]:
    return [outcome for outcome in rows if outcome.verdict == verdict]


def _movement_line(outcome: Outcome) -> str:
    if outcome.rank is None:
        return f"    {outcome.verdict} {outcome.case.id}: {outcome.case.path} no longer returned"
    return (
        f"    {outcome.verdict} {outcome.case.id}: {outcome.case.path} "
        f"rank {outcome.case.baseline_rank} -> {outcome.rank}, "
        f"score {outcome.case.baseline_score:.2f} -> {outcome.score:.2f}"
    )


def _newly_firing_line(outcome: Outcome) -> str:
    return (
        f"    newly firing {outcome.case.id}: nothing applied to this turn, "
        f"top score {outcome.case.baseline_score:.2f} -> {outcome.score:.2f}"
    )


def report(outcomes: Iterable[Outcome], *, threshold: float, cap: int) -> str:
    rows = list(outcomes)
    recall = [outcome for outcome in rows if outcome.case.group == RECALL]
    precision = [outcome for outcome in rows if outcome.case.group == PRECISION]
    delivered = [outcome for outcome in recall if outcome.delivered]
    fired = _with_verdict(precision, "fired")
    newly = [outcome for outcome in fired if not outcome.case.baseline_delivered]
    movements = _with_verdict(recall, "slipped") + _with_verdict(recall, "lost")
    lines = [
        f"recall regression: {len(rows)} cases, threshold {threshold:.2f}, cap {cap}",
        f"  recall set (a usable note existed): {len(recall)} cases",
        f"    delivered {len(delivered):>3}   held {len(_with_verdict(recall, 'held')):>3}"
        f"   slipped {len(movements) - len(_with_verdict(recall, 'lost')):>3}"
        f"   lost {len(_with_verdict(recall, 'lost')):>3}",
        f"  precision set (nothing usable existed): {len(precision)} cases",
        f"    fired {len(fired):>3}   silent {len(precision) - len(fired):>3}"
        f"   newly firing {len(newly):>3}",
    ]
    lines.extend(_movement_line(outcome) for outcome in movements)
    lines.extend(_newly_firing_line(outcome) for outcome in newly)
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
        "--threshold", type=float, default=None, help="defaults to the agent config"
    )
    parser.add_argument("--cap", type=int, default=None, help="defaults to the agent config")
    args = parser.parse_args(argv)
    threshold, cap = injection_policy()
    if args.threshold is not None:
        threshold = args.threshold
    if args.cap is not None:
        cap = args.cap

    decoded: object = json.loads(args.results.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise SystemExit("results must be a JSON object mapping case id to ranked matches")
    payload = cast(dict[str, object], decoded)
    cases = load_cases(args.cases)
    missing = [case.id for case in cases if case.id not in payload]
    if missing:
        raise SystemExit(f"results are missing {len(missing)} case(s), e.g. {missing[0]}")
    outcomes: list[Outcome] = []
    for case in cases:
        ranked = _ranked_from(payload[case.id])
        if case.group == RECALL:
            outcomes.append(evaluate(case, ranked, threshold=threshold, cap=cap))
        else:
            outcomes.append(evaluate_precision(case, ranked, threshold=threshold, cap=cap))
    sys.stdout.write(report(outcomes, threshold=threshold, cap=cap) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
