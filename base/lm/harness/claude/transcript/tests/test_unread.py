# meiva:note
# What the reader says about a row it cannot use is said once for each kind by the process, whatever number of parsers
# the process builds: a page is read again on every poll and each read has a parser of its own.
# meiva:link base/lm/harness/docs/transcript-projection.md

from __future__ import annotations

from base.lm.harness.claude.transcript.window import EntryParser
from base.lm.harness.session.transcript.entries import Anomaly, AnomalyKind, Order
from base.lm.harness.session.transcript.record_file import FileRow, UnreadRows
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


def _row(row_type: str, offset: int) -> FileRow:
    return FileRow(
        offset,
        offset + 1,
        {"type": row_type, "uuid": f"u-{offset}", "timestamp": "2026-11-18T00:00:00Z"},
    )


def test_a_kind_the_reader_does_not_know_is_said_once_by_the_process_not_once_by_every_parser(
    loguru_records: LogRecords,
) -> None:
    unread = UnreadRows()
    for poll in range(3):
        _ = EntryParser(unread).entries([_row("mystery-row", poll)])
    _ = EntryParser(unread).entries([_row("other-mystery-row", 9)])
    said = loguru_records.extras(event="harness_record_row_unread")
    assert sorted(str(extra["detail"]) for extra in said) == ["mystery-row", "other-mystery-row"]
    assert {extra["cli"] for extra in said} == {"Claude Code"}


def test_the_same_kind_of_another_cli_is_a_kind_of_its_own(loguru_records: LogRecords) -> None:
    unread = UnreadRows()
    anomaly = Anomaly(
        key="k", order=Order(0), anomaly=AnomalyKind.UNKNOWN_TYPE, detail="mystery-row"
    )
    for cli in ("Claude Code", "Codex", "Claude Code", "Codex"):
        unread.report(cli, anomaly)
    said = loguru_records.extras(event="harness_record_row_unread")
    assert [extra["cli"] for extra in said] == ["Claude Code", "Codex"]
