from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from xml.etree import ElementTree


# meiva:note
# A collected case's own result is independent of its job and workflow verdict.
# A failed workflow can contain this case's green result; a skip proves collection
# without execution. An unreadable XML document offers neither conclusion.
class CaseOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_COLLECTED = "not-collected-in-available-junit"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class CaseResult:
    test_id: str
    outcome: CaseOutcome


def collected_case_results(xml: str) -> tuple[CaseResult, ...] | None:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    if root.tag not in {"testsuite", "testsuites"}:
        return None
    results: list[CaseResult] = []
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        location = case.get("file") or case.get("classname") or ""
        if not name or not location:
            return None
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = CaseOutcome.FAILED
        elif case.find("skipped") is not None:
            outcome = CaseOutcome.SKIPPED
        else:
            outcome = CaseOutcome.PASSED
        results.append(CaseResult(f"{location}::{name}", outcome))
    return tuple(results)


@dataclass(frozen=True)
class CaseObservation:
    outcome: CaseOutcome
    detail: str
