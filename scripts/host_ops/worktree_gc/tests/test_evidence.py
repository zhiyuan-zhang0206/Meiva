# meiva:note
# Reading another agent's ledger: a fresh line is the judge, an old one is a judge that stopped
# looking, and both have to be told apart from a repo with nothing to reclaim.
# meiva:link scripts/host_ops/docs/worktree-gc.meiva.okf.md

from __future__ import annotations

import datetime
import json
import os

import pytest

from scripts.host_ops.worktree_gc.contracts import ProbeUnavailableError
from scripts.host_ops.worktree_gc.evidence import probe_rows
from scripts.host_ops.worktree_gc.tests.support import settings


def write_ledger(tmp_path, rows) -> None:
    path = tmp_path / "probe-ledger.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def stamp(minutes_ago: float) -> str:
    moment = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes_ago)
    return moment.isoformat(timespec="seconds")


def test_fresh_line_is_the_judge(tmp_path) -> None:
    write_ledger(
        tmp_path,
        [{"ts": stamp(1), "probe_sha": "abc1234", "trees": [{"path": "/w/one", "verdict": "ok"}]}],
    )
    rows, line = probe_rows(settings(tmp_path))
    assert rows == [{"path": "/w/one", "verdict": "ok"}]
    assert line["probe_sha"] == "abc1234"


def test_stale_line_reads_as_no_judge(tmp_path) -> None:
    write_ledger(tmp_path, [{"ts": stamp(120), "trees": [{"path": "/w/one"}]}])
    with pytest.raises(ProbeUnavailableError, match="old"):
        probe_rows(settings(tmp_path))


def test_missing_ledger_reads_as_no_judge(tmp_path) -> None:
    with pytest.raises(ProbeUnavailableError, match="no probe ledger"):
        probe_rows(settings(tmp_path))


def test_line_without_its_own_stamp_is_aged_by_the_file(tmp_path) -> None:
    # meiva:note
    # The fallback is the ledger file's mtime, so a judge that writes no timestamp is still aged
    # rather than trusted forever; two hours of silence is still stale.
    write_ledger(tmp_path, [{"trees": []}])
    path = tmp_path / "probe-ledger.jsonl"
    old = datetime.datetime.now(datetime.UTC).timestamp() - 7200
    os.utime(path, (old, old))
    with pytest.raises(ProbeUnavailableError, match="old"):
        probe_rows(settings(tmp_path))


def test_line_that_is_not_json_reads_as_no_judge(tmp_path) -> None:
    (tmp_path / "probe-ledger.jsonl").write_text("not json\n", encoding="utf-8")
    with pytest.raises(ProbeUnavailableError, match="not JSON"):
        probe_rows(settings(tmp_path))
