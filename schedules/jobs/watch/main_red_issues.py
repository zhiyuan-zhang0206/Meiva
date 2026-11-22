# meiva:link schedules/README.md

from __future__ import annotations

import subprocess
from collections.abc import Callable

from schedules.plans.watch.main_red import Card
from schedules.plans.watch.main_red_issues import (
    MAIN_RED_LABEL,
    MAIN_RED_LABEL_COLOR,
    MAIN_RED_LABEL_DESCRIPTION,
    IssueAction,
    OpenIssue,
    format_new_issue_notice,
    liveness_issues_to_close,
    parse_created_issue_url,
    parse_open_issues,
    plan_open_issue,
)

RunProcess = Callable[..., subprocess.CompletedProcess[str]]

__all__ = ["GithubIssueTracker"]

_GH_TIMEOUT_SECONDS = 60
_OPEN_ISSUES_LIMIT = "200"


class GithubIssueTracker:
    def __init__(
        self,
        repo: str,
        *,
        run_process: RunProcess,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._repo = repo
        self._run_process = run_process
        self._notify = notify
        self._label_ensured = False

    def list_open_issues(self) -> tuple[OpenIssue, ...]:
        done = self._run_process(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self._repo,
                "--label",
                MAIN_RED_LABEL,
                "--state",
                "open",
                "--limit",
                _OPEN_ISSUES_LIMIT,
                "--json",
                "number,title,body",
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        if done.returncode != 0:
            raise RuntimeError(
                f"gh issue list failed (rc={done.returncode}): {(done.stderr or '').strip()}"
            )
        return parse_open_issues(done.stdout, repo=self._repo)

    def ensure_label(self) -> None:
        if self._label_ensured:
            return
        done = self._run_process(
            [
                "gh",
                "label",
                "create",
                MAIN_RED_LABEL,
                "--repo",
                self._repo,
                "--color",
                MAIN_RED_LABEL_COLOR,
                "--description",
                MAIN_RED_LABEL_DESCRIPTION,
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        stderr = (done.stderr or "").strip()
        if done.returncode != 0 and "already exists" not in stderr.lower():
            raise RuntimeError(f"gh label create failed (rc={done.returncode}): {stderr}")
        self._label_ensured = True

    def open_card(self, card: Card) -> int:
        open_issues = self.list_open_issues()
        decision = plan_open_issue(card, open_issues)
        if decision.action == IssueAction.REUSE:
            assert decision.existing is not None
            return decision.existing.number
        self.ensure_label()
        done = self._run_process(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                self._repo,
                "--label",
                decision.label,
                "--title",
                decision.title,
                "--body",
                decision.body,
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        if done.returncode != 0:
            raise RuntimeError(
                f"gh issue create failed (rc={done.returncode}): {(done.stderr or '').strip()}"
            )
        number, url = parse_created_issue_url(done.stdout)
        if self._notify is not None:
            self._notify(format_new_issue_notice(card, number=number, url=url))
        return number

    def close_card(self, issue_number: int, reason: str) -> None:
        done = self._run_process(
            [
                "gh",
                "issue",
                "close",
                str(issue_number),
                "--repo",
                self._repo,
                "--comment",
                reason,
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
        )
        stderr = (done.stderr or "").strip()
        if done.returncode != 0 and "already closed" not in stderr.lower():
            raise RuntimeError(
                f"gh issue close #{issue_number} failed (rc={done.returncode}): {stderr}"
            )

    def close_liveness_cards(self, reason: str = "watch reporting again: a round landed") -> None:
        numbers = liveness_issues_to_close(self.list_open_issues())
        for number in numbers:
            self.close_card(number, reason)
