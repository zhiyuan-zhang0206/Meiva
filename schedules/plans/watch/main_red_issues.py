# meiva:link schedules/README.md

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from schedules.plans.watch.main_red import STALE_WATCH_TITLE, Card

__all__ = [
    "LIVENESS_FINGERPRINT",
    "MAIN_RED_LABEL",
    "MAIN_RED_LABEL_COLOR",
    "MAIN_RED_LABEL_DESCRIPTION",
    "IssueAction",
    "IssueOpenPlan",
    "OpenIssue",
    "fingerprint_marker",
    "format_new_issue_notice",
    "liveness_issues_to_close",
    "parse_created_issue_url",
    "parse_open_issues",
    "plan_open_issue",
]

MAIN_RED_LABEL = "main-red"
MAIN_RED_LABEL_COLOR = "B60205"
MAIN_RED_LABEL_DESCRIPTION = "Post-merge failure or watch alert tracked by main-red-watch"
LIVENESS_FINGERPRINT = "main-red-watch/liveness"

_MAX_ISSUE_TITLE_CHARS = 256
_ISSUE_URL_RE = re.compile(r"(https?://\S+/issues/(\d+))")


@dataclass(frozen=True)
class OpenIssue:
    number: int
    title: str
    body: str
    url: str = ""


class IssueAction(StrEnum):
    CREATE = "create"
    REUSE = "reuse"


@dataclass(frozen=True)
class IssueOpenPlan:
    action: IssueAction
    existing: OpenIssue | None
    title: str
    body: str
    label: str = MAIN_RED_LABEL


def fingerprint_marker(fp: str) -> str:
    return f"<!-- main-red-fingerprint: {fp} -->"


def _issue_title(card: Card) -> str:
    title = card.title.strip()
    if len(title) <= _MAX_ISSUE_TITLE_CHARS:
        return title
    return title[: _MAX_ISSUE_TITLE_CHARS - 3].rstrip() + "..."


def _issue_body(card: Card) -> str:
    marker = fingerprint_marker(card.fingerprint)
    if marker in card.body:
        return card.body
    body = card.body.rstrip()
    return f"{body}\n\n{marker}" if body else marker


def _issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _find_matching_issue(card: Card, open_issues: Sequence[OpenIssue]) -> OpenIssue | None:
    ordered = sorted(open_issues, key=lambda issue: issue.number)
    marker = fingerprint_marker(card.fingerprint)
    for issue in ordered:
        if marker in issue.body:
            return issue
    body_fingerprint = f"Fingerprint: `{card.fingerprint}`"
    for issue in ordered:
        if body_fingerprint in issue.body:
            return issue
    titles = {card.title.strip(), _issue_title(card)}
    for issue in ordered:
        if issue.title.strip() in titles:
            return issue
    return None


def parse_open_issues(raw: str, *, repo: str = "") -> tuple[OpenIssue, ...]:
    payload = object_of(json.loads, raw)
    if not is_object_list(payload):
        raise TypeError("gh issue list output must be a JSON array")
    issues: list[OpenIssue] = []
    for item in payload:
        if not is_object_dict(item):
            continue
        number = item.get("number")
        title = item.get("title")
        body = item.get("body")
        url = item.get("url")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        if not isinstance(title, str):
            continue
        resolved_url = (
            url if isinstance(url, str) and url else (_issue_url(repo, number) if repo else "")
        )
        issues.append(
            OpenIssue(
                number=number,
                title=title,
                body=body if isinstance(body, str) else "",
                url=resolved_url,
            )
        )
    return tuple(sorted(issues, key=lambda issue: issue.number))


def parse_created_issue_url(stdout: str) -> tuple[int, str]:
    match = _ISSUE_URL_RE.search(stdout)
    if match is None:
        raise ValueError(f"gh issue create did not return an issue URL: {stdout.strip()!r}")
    return int(match.group(2)), match.group(1)


def plan_open_issue(card: Card, open_issues: Sequence[OpenIssue]) -> IssueOpenPlan:
    existing = _find_matching_issue(card, open_issues)
    if existing is not None:
        return IssueOpenPlan(
            action=IssueAction.REUSE,
            existing=existing,
            title=existing.title,
            body=existing.body,
            label=MAIN_RED_LABEL,
        )
    return IssueOpenPlan(
        action=IssueAction.CREATE,
        existing=None,
        title=_issue_title(card),
        body=_issue_body(card),
        label=MAIN_RED_LABEL,
    )


def liveness_issues_to_close(open_issues: Sequence[OpenIssue]) -> tuple[int, ...]:
    marker = fingerprint_marker(LIVENESS_FINGERPRINT)
    numbers = [
        issue.number
        for issue in sorted(open_issues, key=lambda item: item.number)
        if marker in issue.body or issue.title.strip() == STALE_WATCH_TITLE
    ]
    return tuple(dict.fromkeys(numbers))


def format_new_issue_notice(card: Card, *, number: int, url: str) -> str:
    highlights = [
        line.strip()
        for line in card.body.splitlines()
        if line.strip().startswith(("Evidence:", "Latest red:", "First seen:", "Class:"))
    ]
    lines = [
        f"[main-red] New GitHub Issue #{number}: {_issue_title(card)}",
        f"Issue: {url}",
        f"Fingerprint: `{card.fingerprint}`",
    ]
    if highlights:
        lines.extend(highlights)
    else:
        first_line = next((line.strip() for line in card.body.splitlines() if line.strip()), "")
        if first_line:
            lines.append(first_line)
    return "\n".join(lines)
