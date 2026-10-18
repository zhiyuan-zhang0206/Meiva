# meiva:note
# Score a judge against a balanced case set next to the two trivial baselines: accuracy must strictly beat max(always-pass, always-fail), and "uncertain" counts as neither correct nor caught so abstaining cannot inflate the score.
# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/meiva-self-evolution.meiva.okf.md

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

Verdict = str
VERDICTS = ("pass", "fail", "uncertain")
Judge = Callable[["Case"], Verdict]


@dataclass(frozen=True)
class Case:
    case_id: str
    task: str
    evidence: tuple[str, ...]
    telemetry: str
    expected: str

    def __post_init__(self) -> None:
        if self.expected not in ("pass", "fail"):
            raise ValueError(
                f"case {self.case_id}: expected must be pass or fail, got {self.expected!r}"
            )


@dataclass(frozen=True)
class Calibration:
    n: int
    expected_pass: int
    expected_fail: int
    accuracy: float
    sensitivity: float
    specificity: float
    abstain_rate: float
    always_pass_accuracy: float
    always_fail_accuracy: float

    @property
    def best_baseline(self) -> float:
        return max(self.always_pass_accuracy, self.always_fail_accuracy)

    @property
    def beats_baselines(self) -> bool:
        return self.accuracy > self.best_baseline


def load_cases(path: str | Path) -> list[Case]:
    raw = cast("list[dict[str, Any]]", json.loads(Path(path).read_text(encoding="utf-8")))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: expected a non-empty JSON array of cases")
    return [
        Case(
            case_id=item["case_id"],
            task=item["task"],
            evidence=tuple(item["evidence"]),
            telemetry=item.get("telemetry", ""),
            expected=item["expected"],
        )
        for item in raw
    ]


def synthesize_failure(case: Case, promise: str, *, case_id: str) -> Case:
    evidence = list(case.evidence)
    if evidence and evidence[0].startswith("Final message"):
        evidence[0] = f"Final message from the agent: {promise}"
    else:
        evidence.insert(0, f"Final message from the agent: {promise}")
    return Case(
        case_id=case_id,
        task=case.task,
        evidence=tuple(evidence),
        telemetry=case.telemetry,
        expected="fail",
    )


def _check_balanced(cases: Sequence[Case]) -> tuple[int, int]:
    if not cases:
        raise ValueError("calibration needs at least one case")
    expected_pass = sum(1 for c in cases if c.expected == "pass")
    expected_fail = len(cases) - expected_pass
    if not expected_pass or not expected_fail:
        raise ValueError(
            "calibration needs both expected-pass and expected-fail cases; a one-sided set scores the sample, not the judge"
        )
    return expected_pass, expected_fail


def _collect_verdicts(cases: Sequence[Case], judge: Judge) -> list[Verdict]:
    verdicts = [judge(c) for c in cases]
    for case, verdict in zip(cases, verdicts, strict=True):
        if verdict not in VERDICTS:
            raise ValueError(
                f"case {case.case_id}: judge returned {verdict!r}, expected one of {VERDICTS}"
            )
    return verdicts


def _tally(cases: Sequence[Case], verdicts: Sequence[Verdict], expected_pass: int) -> Calibration:
    pairs = list(zip(cases, verdicts, strict=True))
    n = len(pairs)
    expected_fail = n - expected_pass
    correct = sum(1 for c, v in pairs if v == c.expected)
    caught = sum(1 for c, v in pairs if c.expected == "fail" and v == "fail")
    cleared = sum(1 for c, v in pairs if c.expected == "pass" and v == "pass")
    abstained = sum(1 for _, v in pairs if v == "uncertain")
    return Calibration(
        n=n,
        expected_pass=expected_pass,
        expected_fail=expected_fail,
        accuracy=round(correct / n, 3),
        sensitivity=round(caught / expected_fail, 3),
        specificity=round(cleared / expected_pass, 3),
        abstain_rate=round(abstained / n, 3),
        always_pass_accuracy=round(expected_pass / n, 3),
        always_fail_accuracy=round(expected_fail / n, 3),
    )


def calibrate(cases: Sequence[Case], judge: Judge) -> Calibration:
    expected_pass, _ = _check_balanced(cases)
    return _tally(cases, _collect_verdicts(cases, judge), expected_pass)


def format_report(cal: Calibration) -> str:
    verdict = (
        "beats both baselines" if cal.beats_baselines else "does NOT beat the trivial baselines"
    )
    return "\n".join(
        [
            f"cases: n={cal.n} expected_pass={cal.expected_pass} expected_fail={cal.expected_fail}",
            f"judge: accuracy={cal.accuracy:.3f} sensitivity={cal.sensitivity:.3f} "
            f"specificity={cal.specificity:.3f} abstain={cal.abstain_rate:.3f}",
            f"baseline always-pass={cal.always_pass_accuracy:.3f} always-fail={cal.always_fail_accuracy:.3f} "
            f"best={cal.best_baseline:.3f}",
            f"verdict: {verdict}",
        ]
    )


def stub_judge(name: str) -> Judge:
    stubs: dict[str, Judge] = {
        "always-pass": lambda _case: "pass",
        "always-fail": lambda _case: "fail",
        "always-uncertain": lambda _case: "uncertain",
        "perfect": lambda case: case.expected,
        "inverted": lambda case: "fail" if case.expected == "pass" else "pass",
    }
    if name not in stubs:
        raise ValueError(f"unknown stub judge {name!r}; choose from {sorted(stubs)}")
    return stubs[name]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a judge against trivial baselines on a balanced case set."
    )
    parser.add_argument("--cases", required=True, help="JSON case file")
    parser.add_argument("--judge", default="always-pass", help="stub name")
    args = parser.parse_args(list(argv) if argv is not None else None)
    cal = calibrate(load_cases(args.cases), stub_judge(args.judge))
    print(format_report(cal))
    return 0 if cal.beats_baselines else 1


if __name__ == "__main__":
    raise SystemExit(main())
