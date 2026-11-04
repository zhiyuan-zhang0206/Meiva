from __future__ import annotations

import os
from pathlib import Path

import pytest

from base.deploy.lifecycle import status_journal
from cli.commands.lifecycle.stop.report import finish_stop
from cli.commands.lifecycle.tests.stop.stop_support import finished_journal, journal_result
from cli.commands.lifecycle.tests.stop.stop_support import home as home

NOTE = "postgres fast shutdown did not complete within 277s; killed leftover processes: 4242"


def test_an_escalated_shutdown_is_in_the_report_and_the_journal(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert status_journal.begin(home, "stop", pid=os.getpid())

    assert finish_stop(home, owns_journal=True, notes=[NOTE], clients=[]) == 0

    assert f"Stop completed with an escalation: {NOTE}" in capsys.readouterr().err
    journal = finished_journal(home)
    assert journal.complete
    assert journal_result(journal)["rc"] == 0
    assert journal_result(journal)["escalations"] == [NOTE]


def test_a_stop_without_an_escalation_says_nothing_extra(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert status_journal.begin(home, "stop", pid=os.getpid())

    assert finish_stop(home, owns_journal=True, notes=[], clients=[]) == 0

    assert capsys.readouterr().err == ""
    assert "escalations" not in journal_result(finished_journal(home))


def test_pooler_clients_at_the_stop_land_in_the_journal(home: Path) -> None:
    line = "1 client(s) still connected when the pooler stops: 10.1.2.7:5432 db=meiva"
    assert status_journal.begin(home, "stop", pid=os.getpid())

    assert finish_stop(home, owns_journal=True, notes=[], clients=[line]) == 0

    result = journal_result(finished_journal(home))
    assert result["pooler_clients"] == [line]
    assert "escalations" not in result
