from __future__ import annotations

import re
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
        identity = case_identity(case.get("file"), case.get("classname"), case.get("name") or "")
        if identity is None:
            return None
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = CaseOutcome.FAILED
        elif case.find("skipped") is not None:
            outcome = CaseOutcome.SKIPPED
        else:
            outcome = CaseOutcome.PASSED
        results.append(CaseResult(identity, outcome))
    return tuple(results)


@dataclass(frozen=True)
class CaseObservation:
    outcome: CaseOutcome
    detail: str


# meiva:note
# Pytest mangle_test_address replaces path separators with dots and removes
# .py. Reverse that for a module-shaped test filename when xunit2 omits file;
# class-qualified or unfamiliar classnames retain their original identity.
def case_identity(file: str | None, classname: str | None, name: str) -> str | None:
    location = file or classname or ""
    if not file and classname and "." in classname:
        parts = classname.split(".")
        if all(part.isidentifier() for part in parts) and re.fullmatch(
            r"test_[A-Za-z0-9_]+|[a-z][a-z0-9_]*_test", parts[-1]
        ):
            location = "/".join(parts) + ".py"
    return f"{location}::{name}" if location and name else None
