# meiva:link schedules/README.md

from __future__ import annotations

import ast
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from base.db import Database
from schedules.jobs.watch.main_red_issues import GithubIssueTracker
from schedules.jobs.watch.main_red_liveness import liveness_once
from schedules.jobs.watch.main_red_watch import STATE_FILE, load_state, watch_once
from schedules.plans.watch.main_red import (
    FAILURE_TEST,
    STALE_WATCH_TITLE,
    Card,
    Failure,
    RunFacts,
)
from schedules.plans.watch.main_red_issues import (
    LIVENESS_FINGERPRINT,
    MAIN_RED_LABEL,
    IssueAction,
    OpenIssue,
    fingerprint_marker,
    format_new_issue_notice,
    liveness_issues_to_close,
    parse_created_issue_url,
    parse_open_issues,
    plan_open_issue,
)

_REPO = "zhiyuan-zhang0206/Meiva"
_SCHEDULES_DIR = Path(__file__).resolve().parents[2]


class _FakeGh:
    def __init__(
        self,
        *,
        initial_issues: list[dict[str, object]] | None = None,
        next_number: int = 101,
        already_closed_on_close: bool = False,
    ) -> None:
        self.issues: dict[int, dict[str, object]] = {
            int(str(item["number"])): dict(item) for item in (initial_issues or [])
        }
        self.next_number = next_number
        self.already_closed_on_close = already_closed_on_close
        self.calls: list[list[str]] = []
        self.labels_created: list[str] = []
        self.closed_comments: list[tuple[int, str]] = []

    def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args[:3] == ["gh", "issue", "list"]:
            open_list = [
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "body": issue["body"],
                }
                for issue in sorted(self.issues.values(), key=lambda row: int(str(row["number"])))
                if issue.get("state", "open") == "open"
            ]
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(open_list), stderr="")
        if args[:3] == ["gh", "label", "create"]:
            self.labels_created.append(args[3])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["gh", "issue", "create"]:
            title = args[args.index("--title") + 1]
            body = args[args.index("--body") + 1]
            label = args[args.index("--label") + 1]
            number = self.next_number
            self.next_number += 1
            self.issues[number] = {
                "number": number,
                "title": title,
                "body": body,
                "label": label,
                "state": "open",
            }
            url = f"https://github.com/{_REPO}/issues/{number}\n"
            return subprocess.CompletedProcess(args, 0, stdout=url, stderr="")
        if args[:3] == ["gh", "issue", "close"]:
            number = int(args[3])
            comment = args[args.index("--comment") + 1]
            self.closed_comments.append((number, comment))
            if self.already_closed_on_close:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr=f"issue #{number} is already closed\n"
                )
            if number in self.issues:
                self.issues[number]["state"] = "closed"
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected gh command: {args}")


def _sample_card(
    *,
    fp: str = "release-gate.yml::pkg/tests/test_a.py::test_x",
    title: str = "main-red: pkg/tests/test_a.py::test_x failed 2 post-merge runs on main (release-gate.yml)",
    body: str | None = None,
    episode: str = "2026-11-22T06:00:00+00:00",
) -> Card:
    default_body = (
        f"Fingerprint: `{fp}`\n\n"
        "Evidence: https://github.com/zhiyuan-zhang0206/Meiva/actions/runs/999 (jobs: Backend)\n\n"
        "First seen: `aaa111` at 2026-11-22T06:00:00+00:00\n\n"
        "Latest red: `bbb222` at 2026-11-22T12:00:00+00:00\n\n"
        "Class: `test`"
    )
    return Card(fp, title, body if body is not None else default_body, episode)


def test_pure_plan_embeds_fingerprint_marker_and_truncates_long_title() -> None:
    long_title = "main-red: " + "a" * 300
    card = _sample_card(title=long_title)

    plan = plan_open_issue(card, ())

    assert plan.action == IssueAction.CREATE
    assert len(plan.title) == 256
    assert plan.title.endswith("...")
    assert fingerprint_marker(card.fingerprint) in plan.body
    again = plan_open_issue(Card(card.fingerprint, card.title, plan.body, card.episode), ())
    assert again.body == plan.body


def test_pure_plan_dedupes_by_marker_body_fingerprint_and_title() -> None:
    card = _sample_card()
    by_marker = OpenIssue(
        number=55,
        title="main-red: old title with 1 red",
        body=f"details\n\n{fingerprint_marker(card.fingerprint)}",
    )
    plan = plan_open_issue(card, [OpenIssue(number=99, title="other", body=""), by_marker])
    assert plan.action == IssueAction.REUSE
    assert plan.existing == by_marker

    by_line = OpenIssue(
        number=66,
        title="manual title",
        body=f"Fingerprint: `{card.fingerprint}`\n\nmanual issue",
    )
    assert plan_open_issue(card, [by_line]).existing == by_line

    by_title = OpenIssue(number=77, title=card.title, body="no fingerprint here")
    assert plan_open_issue(card, [by_title]).existing == by_title

    fresh = plan_open_issue(card, [OpenIssue(number=88, title="unrelated", body="unrelated")])
    assert fresh.action == IssueAction.CREATE
    assert fresh.existing is None
    assert fresh.label == MAIN_RED_LABEL
    assert fingerprint_marker(card.fingerprint) in fresh.body


def test_pure_parsers_and_liveness_selector() -> None:
    parsed = parse_open_issues(
        json.dumps(
            [
                {"number": 20, "title": STALE_WATCH_TITLE, "body": ""},
                {
                    "number": 10,
                    "title": "custom stale title",
                    "body": fingerprint_marker(LIVENESS_FINGERPRINT),
                },
                {"number": 15, "title": "main-red: other", "body": "Fingerprint: `ci.yml::x`"},
                {"number": "bad", "title": "skip", "body": ""},
            ]
        ),
        repo=_REPO,
    )
    assert [issue.number for issue in parsed] == [10, 15, 20]
    assert parsed[0].url == f"https://github.com/{_REPO}/issues/10"
    assert liveness_issues_to_close(parsed) == (10, 20)

    with pytest.raises(TypeError):
        parse_open_issues("{}")

    assert parse_created_issue_url(f"https://github.com/{_REPO}/issues/4321\n") == (
        4321,
        f"https://github.com/{_REPO}/issues/4321",
    )
    with pytest.raises(ValueError):
        parse_created_issue_url("no url here")


def test_format_new_issue_notice_includes_link_and_key_evidence() -> None:
    card = _sample_card()
    url = f"https://github.com/{_REPO}/issues/321"
    notice = format_new_issue_notice(card, number=321, url=url)

    assert "#321" in notice
    assert url in notice
    assert card.fingerprint in notice
    assert "Evidence:" in notice
    assert "Latest red:" in notice


def test_tracker_creates_issue_once_notifies_lead_and_dedupes_repeats() -> None:
    gh = _FakeGh(next_number=501)
    notices: list[str] = []
    tracker = GithubIssueTracker(_REPO, run_process=gh, notify=notices.append)
    card = _sample_card()

    first_num = tracker.open_card(card)
    assert first_num == 501
    assert gh.labels_created == [MAIN_RED_LABEL]
    assert len(notices) == 1
    assert f"https://github.com/{_REPO}/issues/501" in notices[0]
    create_calls = [cmd for cmd in gh.calls if cmd[:3] == ["gh", "issue", "create"]]
    assert len(create_calls) == 1
    assert "--label" in create_calls[0] and MAIN_RED_LABEL in create_calls[0]

    repeat_card = _sample_card(
        title="main-red: pkg/tests/test_a.py::test_x failed 3 post-merge runs on main (release-gate.yml)"
    )
    second_num = tracker.open_card(repeat_card)
    assert second_num == 501
    assert len([cmd for cmd in gh.calls if cmd[:3] == ["gh", "issue", "create"]]) == 1
    assert len(notices) == 1, "deduped open issue must not re-notify lead"


def test_tracker_closes_issue_and_tolerates_already_closed() -> None:
    gh = _FakeGh(already_closed_on_close=True)
    tracker = GithubIssueTracker(_REPO, run_process=gh)

    tracker.close_card(501, "red ended: resolved by a green run")
    assert gh.closed_comments == [(501, "red ended: resolved by a green run")]


def test_watch_once_end_to_end_with_mocked_gh_opens_dedupes_and_closes_on_green(
    tmp_path: Path,
) -> None:
    gh = _FakeGh(next_number=701)
    notices: list[str] = []
    tracker = GithubIssueTracker(_REPO, run_process=gh, notify=notices.append)
    state_path = tmp_path / STATE_FILE
    t0 = datetime(2026, 11, 22, 6, tzinfo=UTC)

    def run_round(runs: tuple[RunFacts, ...], now: datetime) -> str:
        return watch_once(
            state_path=state_path,
            now=now,
            fetch=lambda _w, _s=None, _a=None: runs,
            last_start=lambda _w: now - timedelta(hours=1),
            open_card=tracker.open_card,
            close_card=tracker.close_card,
            emit=lambda _p: None,
            workflows=("release-gate.yml",),
            liveness=(),
            notify=None,
        )

    red_1 = RunFacts(
        "release-gate.yml",
        "sha1",
        t0,
        "failure",
        (Failure("pkg/tests/test_a.py::test_x", FAILURE_TEST, job="Backend"),),
        html_url="https://github.com/zhiyuan-zhang0206/Meiva/actions/runs/1",
        job_names=("Backend",),
    )
    red_2 = RunFacts(
        "release-gate.yml",
        "sha2",
        t0 + timedelta(hours=6),
        "failure",
        (Failure("pkg/tests/test_a.py::test_x", FAILURE_TEST, job="Backend"),),
        html_url="https://github.com/zhiyuan-zhang0206/Meiva/actions/runs/2",
        job_names=("Backend",),
    )
    red_3 = RunFacts(
        "release-gate.yml",
        "sha3",
        t0 + timedelta(hours=12),
        "failure",
        (Failure("pkg/tests/test_a.py::test_x", FAILURE_TEST, job="Backend"),),
        html_url="https://github.com/zhiyuan-zhang0206/Meiva/actions/runs/3",
        job_names=("Backend",),
    )
    green = RunFacts("release-gate.yml", "sha4", t0 + timedelta(hours=18), "success", ())

    run_round((red_1,), t0 + timedelta(minutes=5))
    assert gh.issues == {}
    assert notices == []

    line = run_round((red_2,), t0 + timedelta(hours=6, minutes=5))
    assert "opened issue #701" in line
    assert list(gh.issues) == [701]
    assert gh.issues[701]["state"] == "open"
    assert len(notices) == 1
    assert "https://github.com/zhiyuan-zhang0206/Meiva/issues/701" in notices[0]
    (entry,) = load_state(state_path).entries.values()
    assert entry.issue_number == 701

    run_round((red_3,), t0 + timedelta(hours=12, minutes=5))
    assert list(gh.issues) == [701]
    assert len(notices) == 1

    green_line = run_round((green,), t0 + timedelta(hours=18, minutes=5))
    assert "closed issue #701" in green_line
    assert gh.issues[701]["state"] == "closed"
    assert gh.closed_comments == [(701, "red ended: resolved by a green run")]
    assert load_state(state_path).entries == {}


def test_liveness_opens_dedupes_and_closes_github_issue() -> None:
    gh = _FakeGh(next_number=801)
    notices: list[str] = []
    tracker = GithubIssueTracker(_REPO, run_process=gh, notify=notices.append)
    last_round = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)

    class _FakeDb:
        def __init__(self, ts: datetime) -> None:
            self._ts = ts

        def connect(self, *, autocommit: bool = False) -> _FakeDb:
            assert autocommit
            return self

        def __enter__(self) -> _FakeDb:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def cursor(self) -> _FakeDb:
            return self

        def execute(self, _sql: str, _params: object) -> None:
            return None

        def fetchone(self) -> tuple[datetime]:
            return (self._ts,)

    first = liveness_once(
        last_round + timedelta(minutes=65),
        database=cast("Database", _FakeDb(last_round)),
        open_card=tracker.open_card,
        close_card=tracker.close_liveness_cards,
    )
    second = liveness_once(
        last_round + timedelta(minutes=95),
        database=cast("Database", _FakeDb(last_round)),
        open_card=tracker.open_card,
        close_card=tracker.close_liveness_cards,
    )
    assert first == 801
    assert second == 801
    assert list(gh.issues) == [801]
    assert len(notices) == 1

    recovered = liveness_once(
        last_round + timedelta(minutes=125),
        database=cast("Database", _FakeDb(last_round + timedelta(minutes=120))),
        open_card=tracker.open_card,
        close_card=tracker.close_liveness_cards,
    )
    assert recovered is None
    assert gh.issues[801]["state"] == "closed"
    assert gh.closed_comments == [(801, "watch reporting again: a round landed")]


def test_legacy_state_json_with_task_id_loads_safely_without_closing_wrong_issue(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / STATE_FILE
    legacy_payload: dict[str, object] = {
        "entries": {
            "release-gate.yml::pkg/tests/test_a.py::test_x": {
                "state": "confirmed",
                "first_seen_sha": "sha1",
                "first_seen_at": "2026-11-17T01:45:57+00:00",
                "last_red_sha": "sha2",
                "last_red_at": "2026-11-17T06:45:57+00:00",
                "red_count": 2,
                "last_green_sha": None,
                "last_green_at": None,
                "task_id": 6817,
                "kind": "test",
                "job": "Backend",
                "run_id": 43998040023,
                "opportunities": 0,
                "review_run_id": 0,
                "review_outcome": None,
                "review_detail": "",
                "covers_full_suite": True,
                "artifact_candidates": list[str](),
                "artifact_note": "",
            },
            "ci.yml::pkg/tests/test_b.py::test_y": {
                "state": "suspected",
                "first_seen_sha": "sha1",
                "first_seen_at": "2026-11-17T01:45:57+00:00",
                "last_red_sha": "sha1",
                "last_red_at": "2026-11-17T01:45:57+00:00",
                "red_count": 1,
                "task_id": None,
            },
        },
        "watermarks": {"release-gate.yml": "2026-11-17T06:45:57+00:00|1"},
        "first_seen": {"release-gate.yml": "2026-11-01T00:00:00+00:00"},
        "greens": dict[str, str](),
        "slot": {"gap_id": "2026-11-16T00:00:00+00:00", "task_id": 6818, "unreadable_rounds": 0},
        "blind": {"since": "2026-11-16T00:00:00+00:00", "task_id": 6819, "rounds": 1},
    }
    state_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    loaded = load_state(state_path)
    assert loaded.entries["release-gate.yml::pkg/tests/test_a.py::test_x"].issue_number is None
    assert loaded.entries["ci.yml::pkg/tests/test_b.py::test_y"].issue_number is None
    assert loaded.slot.issue_number is None
    assert loaded.blind.issue_number is None

    closed: list[tuple[int, str]] = []
    watch_once(
        state_path=state_path,
        now=datetime(2026, 11, 22, 12, tzinfo=UTC),
        fetch=lambda _w, _s=None, _a=None: (
            RunFacts(
                "release-gate.yml", "sha3", datetime(2026, 11, 22, 11, tzinfo=UTC), "success", ()
            ),
        ),
        last_start=lambda _w: datetime(2026, 11, 22, 11, tzinfo=UTC),
        open_card=lambda _c: 999,
        close_card=lambda num, reason: closed.append((num, reason)),
        emit=lambda _p: None,
        workflows=("release-gate.yml",),
        liveness=(),
        notify=None,
    )
    assert closed == []
    saved_raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert "task_id" not in saved_raw["slot"]
    assert "issue_number" in saved_raw["slot"]
    assert "task_id" not in saved_raw["blind"]
    assert "issue_number" in saved_raw["blind"]


def test_schedules_has_zero_meiva_tasks_or_task_registry_dependencies() -> None:
    offenders: list[str] = []
    for path in sorted(_SCHEDULES_DIR.rglob("*.py")):
        if "tests" in path.relative_to(_SCHEDULES_DIR).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if (
                    "meiva_fleet" in node.module
                    or "task_registry" in node.module
                    or node.module == "meiva.tasks"
                ):
                    offenders.append(
                        f"{path.relative_to(_SCHEDULES_DIR)}:{node.lineno}: from {node.module}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if (
                        "meiva_fleet" in alias.name
                        or "task_registry" in alias.name
                        or alias.name == "meiva.tasks"
                    ):
                        offenders.append(
                            f"{path.relative_to(_SCHEDULES_DIR)}:{node.lineno}: import {alias.name}"
                        )
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "tasks"
                and isinstance(node.value, ast.Name)
                and node.value.id == "meiva"
            ):
                offenders.append(f"{path.relative_to(_SCHEDULES_DIR)}:{node.lineno}: meiva.tasks")
    assert offenders == [], f"retired meiva.tasks references remain in schedules/: {offenders}"
