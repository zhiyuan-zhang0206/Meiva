# meiva:note
# `meiva schedules verify` as an operator runs it: the rows come from a real gateway listener (or a
# rows file), every script is dry-imported by a real child process, and the failure signal lands in
# a real event pipeline. The decisions -- which rows are red, the exact lines, whether a signal is
# owed -- are `sweep_outcome`'s and `child_verdict`'s, asserted as values in their own tests.
# meiva:link cli/docs/operator-surfaces/operator-surfaces.meiva.okf.md
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from base.host.net.tests.local_http import closed_port_url
from base.telemetry import EventPipeline
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.cluster.tests.gateway_server import SECRET, Reply, access_to, serving
from cli.commands.management import schedules_verify as _verify
from cli.commands.management.child_verdict import (
    child_verdict,
    spawn_failure_verdict,
    timeout_verdict,
)
from cli.commands.management.schedules_verify import (
    CheckFile,
    GatewayUnresolved,
    RowsFromFile,
    RowsFromGateway,
)
from cli.commands.management.sweep_outcome import (
    Red,
    StoredSchedule,
    failure_event,
    needs_check,
    summarize,
    unreadable,
)

_NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
_STAMP = "2026-10-24T12:00:00+00:00"


def _gateway_rows(rows: Sequence[tuple[int, str, str | None]]) -> dict[tuple[str, str], Reply]:
    routes = {("GET", "/api/schedules"): Reply(body=[{"id": i, "name": n} for i, n, _ in rows])}
    for schedule_id, name, script in rows:
        detail = {"id": schedule_id, "name": name, "script": script}
        routes[("GET", f"/api/schedules/{schedule_id}")] = Reply(body=detail)
    return routes


def _verify_failed(events: EmittedEvents) -> dict[str, object]:
    (event,) = events
    assert (event.category, event.event_name, event.level) == (
        "telemetry",
        "schedule_verify_failed",
        "error",
    )
    return event.attributes


def _sweep(
    source: _verify.SweepSource, producer: Callable[[], EventPipeline], *, notify: bool = True
) -> int:
    return _verify.cmd_schedules_verify(source=source, notify=notify, now=_NOW, producer=producer)


def test_a_gateway_of_clean_scripts_prints_the_result_line_and_reports_nothing(
    capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    rows = [(1, "a", "print(1)\n"), (2, "b", "print(2)\n")]
    with serving(_gateway_rows(rows)) as gateway:
        code = _sweep(RowsFromGateway(access_to(gateway.url)), emitted_events.producer)
    assert code == 0
    assert f"RESULT ts={_STAMP} checked=2 green=2 red=0 rc=0" in capsys.readouterr().out
    assert list(emitted_events) == []


def test_each_red_script_is_named_and_the_alert_pipeline_is_told(
    capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    rows = [
        (1, "clean", "print(1)\n"),
        (7, "drifted", "import zz_meiva_gone\n"),
        (9, "blank", "   \n"),
        (11, "stopped-without-a-script", None),
    ]
    with serving(_gateway_rows(rows)) as gateway:
        code = _sweep(RowsFromGateway(access_to(gateway.url)), emitted_events.producer)
    assert code == 1
    out = capsys.readouterr().out
    assert "checked=4 green=3 red=1 rc=1" in out
    assert "RED id=7 name=drifted missing=zz_meiva_gone" in out
    event = _verify_failed(emitted_events)
    assert event["checked"] == 4 and event["red"] == 1 and event["tool_error"] is None
    assert "id=7" in str(event["detail"]) and "zz_meiva_gone" in str(event["detail"])


def test_notify_off_prints_the_reds_and_tells_nobody(
    capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    with serving(_gateway_rows([(7, "x", "import zz_meiva_gone\n")])) as gateway:
        code = _sweep(
            RowsFromGateway(access_to(gateway.url)), emitted_events.producer, notify=False
        )
    assert code == 1
    assert "RED id=7 name=x missing=zz_meiva_gone" in capsys.readouterr().out
    assert list(emitted_events) == []


def test_a_gateway_that_cannot_be_dialed_is_a_tool_error_not_a_clean_sweep(
    capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    code = _sweep(RowsFromGateway(access_to(closed_port_url())), emitted_events.producer)
    assert code == 2
    out = capsys.readouterr().out
    assert f"RESULT ts={_STAMP} checked=0 green=0 red=0 rc=2" in out
    assert "TOOL-ERROR ConnectError" in out
    event = _verify_failed(emitted_events)
    assert event["checked"] == 0 and str(event["tool_error"]).startswith("ConnectError")


def test_a_gateway_identity_that_cannot_be_resolved_is_a_tool_error_with_a_signal(
    capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    unresolved = GatewayUnresolved("GatewayApiBaseMissing", "gateway_url unset")
    assert _sweep(unresolved, emitted_events.producer) == 2
    assert "TOOL-ERROR GatewayApiBaseMissing: gateway_url unset" in capsys.readouterr().out
    assert (
        _verify_failed(emitted_events)["tool_error"] == "GatewayApiBaseMissing: gateway_url unset"
    )


def test_a_gateway_that_refuses_the_credential_is_a_tool_error(
    capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    with serving({("GET", "/api/schedules"): Reply(status=401, body={})}) as gateway:
        code = _sweep(RowsFromGateway(access_to(gateway.url)), emitted_events.producer)
    assert code == 2
    assert "TOOL-ERROR HTTPStatusError" in capsys.readouterr().out
    assert _verify_failed(emitted_events)["red"] == 0


def test_the_rows_are_read_in_id_order_with_the_credential_and_an_empty_script_for_none() -> None:
    rows = [(9, "b", None), (2, "a", "print(1)\n")]
    with serving(_gateway_rows(rows)) as gateway:
        read = _verify.read_schedule_rows(access_to(gateway.url))
        seen = gateway.requests
    assert read == [(2, "a", "print(1)\n"), (9, "b", "")]
    assert [request.target for request in seen] == [
        "/api/schedules",
        "/api/schedules/2",
        "/api/schedules/9",
    ]
    assert {request.headers["authorization"] for request in seen} == {f"Bearer {SECRET}"}


def test_a_refused_row_read_raises_the_status_error() -> None:
    with (
        serving({("GET", "/api/schedules"): Reply(status=401, body={})}) as gateway,
        pytest.raises(httpx.HTTPStatusError),
    ):
        _verify.read_schedule_rows(access_to(gateway.url))


def test_a_rows_file_sweeps_the_dump_without_the_gateway_or_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([[3, "ok", "import os\n"], [4, "stale", "import zz_meiva_gone\n"]]))
    assert _sweep(RowsFromFile(str(rows)), emitted_events.producer) == 1
    out = capsys.readouterr().out
    assert "checked=2 green=1 red=1 rc=1" in out
    assert "RED id=4 name=stale missing=zz_meiva_gone" in out
    assert list(emitted_events) == []


def test_a_rows_file_overlays_builtin_templates_while_keeping_custom_rows_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    rows = tmp_path / "rows.json"
    rows.write_text(
        json.dumps(
            [
                [1, "self-evolution-weekly", "import zz_meiva_old_builtin_snapshot\n"],
                [9, "custom-agent-job", "import zz_meiva_custom_missing\n"],
            ]
        )
    )
    assert _sweep(RowsFromFile(str(rows)), emitted_events.producer) == 1
    out = capsys.readouterr().out
    assert "checked=2 green=1 red=1 rc=1" in out
    assert "name=self-evolution-weekly" not in out
    assert "RED id=9 name=custom-agent-job missing=zz_meiva_custom_missing" in out
    assert list(emitted_events) == []


@pytest.mark.parametrize(
    ("content", "error"),
    [("not json", "JSONDecodeError"), (None, "FileNotFoundError"), ('[[1, "x"]]', "IndexError")],
)
def test_a_rows_file_that_cannot_be_read_is_a_tool_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    emitted_events: EmittedEvents,
    content: str | None,
    error: str,
) -> None:
    rows = tmp_path / "rows.json"
    if content is not None:
        rows.write_text(content)
    assert _sweep(RowsFromFile(str(rows)), emitted_events.producer) == 2
    out = capsys.readouterr().out
    assert "checked=0 green=0 red=0 rc=2" in out and f"TOOL-ERROR {error}" in out
    assert list(emitted_events) == []


def test_check_file_says_ok_red_and_a_syntax_error_in_its_own_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    good = tmp_path / "good.py"
    good.write_text("import os\n", encoding="utf-8")
    assert _sweep(CheckFile(str(good)), emitted_events.producer) == 0
    assert "CHECK-OK" in capsys.readouterr().out

    bad = tmp_path / "bad.py"
    bad.write_text("import zz_meiva_verify_missing\n", encoding="utf-8")
    assert _sweep(CheckFile(str(bad)), emitted_events.producer) == 1
    assert "CHECK-RED missing=zz_meiva_verify_missing" in capsys.readouterr().out

    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")
    assert _sweep(CheckFile(str(broken)), emitted_events.producer) == 1
    assert "CHECK-RED missing=compile-error:" in capsys.readouterr().out
    assert list(emitted_events) == []


def test_check_file_that_cannot_be_read_is_a_tool_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], emitted_events: EmittedEvents
) -> None:
    assert _sweep(CheckFile(str(tmp_path / "missing.py")), emitted_events.producer) == 2
    assert "cannot read script file" in capsys.readouterr().err

    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\xff\xfe\x00")
    assert _sweep(CheckFile(str(binary)), emitted_events.producer) == 2
    assert "cannot read script file" in capsys.readouterr().err


def test_the_failure_event_names_the_reds(emitted_events: EmittedEvents) -> None:
    outcome = summarize(_STAMP, ((StoredSchedule(7, "drifted", "x"), "shared.watcher"),))
    _verify.report_verify(outcome, producer=emitted_events.producer)
    event = _verify_failed(emitted_events)
    assert event["checked"] == 1 and event["red"] == 1 and event["tool_error"] is None
    assert "id=7" in str(event["detail"]) and "shared.watcher" in str(event["detail"])


def test_the_failure_event_of_an_unreadable_table_names_the_error(
    emitted_events: EmittedEvents,
) -> None:
    _verify.report_verify(
        unreadable(_STAMP, "RuntimeError", "db down"), producer=emitted_events.producer
    )
    event = _verify_failed(emitted_events)
    assert event["red"] == 0 and event["tool_error"] == "RuntimeError: db down"
    assert "RuntimeError: db down" in str(event["detail"])


# meiva:note
# The plan, with no gateway and no child: which rows count, the lines and the exit code.
def test_a_row_with_nothing_to_import_is_green_and_every_row_counts_as_checked() -> None:
    assert not needs_check("") and not needs_check(" \n\t") and needs_check("x = 1")
    rows = (StoredSchedule(1, "a", ""), StoredSchedule(2, "b", "import zz"))
    outcome = summarize(_STAMP, ((rows[0], None), (rows[1], "zz")))
    assert outcome.lines == (
        f"RESULT ts={_STAMP} checked=2 green=1 red=1 rc=1",
        "RED id=2 name=b missing=zz",
    )
    assert (outcome.rc, outcome.checked, outcome.reds) == (1, 2, (Red(2, "b", "zz"),))
    assert outcome.owes_signal and outcome.tool_error is None


def test_a_clean_sweep_exits_zero_and_owes_no_signal() -> None:
    outcome = summarize(_STAMP, ((StoredSchedule(1, "a", "x"), None),))
    assert outcome.rc == 0 and outcome.lines == (
        f"RESULT ts={_STAMP} checked=1 green=1 red=0 rc=0",
    )
    assert not outcome.owes_signal


def test_an_empty_table_is_a_clean_sweep_of_nothing() -> None:
    assert summarize(_STAMP, ()).lines == (f"RESULT ts={_STAMP} checked=0 green=0 red=0 rc=0",)


def test_unreadable_rows_exit_two_with_the_error_truncated_and_owe_a_signal() -> None:
    outcome = unreadable(_STAMP, "OperationalError", "x" * 400)
    assert outcome.rc == 2 and outcome.checked == 0 and outcome.owes_signal
    assert outcome.lines[0] == f"RESULT ts={_STAMP} checked=0 green=0 red=0 rc=2"
    assert outcome.lines[1] == f"TOOL-ERROR OperationalError: {'x' * 160}"


def test_the_event_detail_of_reds_counts_them_against_the_rows_checked() -> None:
    outcome = summarize(
        _STAMP,
        ((StoredSchedule(1, "a", "x"), "m1"), (StoredSchedule(2, "b", "y"), "m2")),
    )
    event = failure_event(outcome)
    assert (event.checked, event.red, event.tool_error) == (2, 2, None)
    assert event.detail == (
        "2/2 in-store schedule script(s) failed dry-import: "
        "id=1 name=a missing=m1; id=2 name=b missing=m2"
    )


def test_the_event_detail_of_an_unreadable_table_is_its_tool_error() -> None:
    event = failure_event(unreadable(_STAMP, "RuntimeError", "db down"))
    assert event.detail == "tool-error: RuntimeError: db down"
    assert (event.checked, event.red) == (0, 0)


@pytest.mark.parametrize(
    ("stdout", "stderr", "rc", "verdict"),
    [
        ("CHILD-OK\n", "", 0, None),
        ("noise\nCHILD-OK\n\n", "", 0, None),
        ("CHILD-MODULE:shared.watcher\n", "", 1, "shared.watcher"),
        ("CHILD-EXC:ImportError: boom\n", "", 1, "ImportError: boom"),
        ("CHILD-COMPILE-ERROR:3:invalid syntax\n", "", 1, "compile-error:3:invalid syntax"),
        ("CHILD-UNDEF:L2 base\n", "", 1, "undefined-name:L2 base"),
        (
            "CHILD-SIG:L16 fire_slot_once(): missing\n",
            "",
            1,
            "call-signature:L16 fire_slot_once(): missing",
        ),
        ("", "Traceback...\nValueError: nope\n", 1, "tool-error:ValueError: nope"),
        ("", "", 7, "tool-error:rc=7"),
        ("CHILD-WHAT:x\n", "", 0, "tool-error:rc=0"),
    ],
)
def test_the_childs_last_stdout_line_is_the_verdict(
    stdout: str, stderr: str, rc: int, verdict: str | None
) -> None:
    assert child_verdict(stdout, stderr, rc) == verdict


def test_a_child_with_no_verdict_carries_only_a_bounded_tail_of_its_stderr() -> None:
    assert child_verdict("", "e" * 500, 1) == f"tool-error:{'e' * 140}"


def test_a_wedged_or_unspawnable_child_is_a_red_detail() -> None:
    assert timeout_verdict(90.0) == "timeout>90s"
    assert spawn_failure_verdict("FileNotFoundError", "m" * 300) == (
        f"tool-error:FileNotFoundError:{'m' * 100}"
    )
