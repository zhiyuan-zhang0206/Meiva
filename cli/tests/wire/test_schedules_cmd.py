# meiva:link cli/docs/operator-surfaces/public-client-wire.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cli.commands.management import schedules as _sched
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.tests.wire.public_gateway import PublicGateway
from cli.tests.wire.public_gateway import wire as wire


def _row(
    schedule_id: int = 7,
    name: str = "nightly",
    *,
    enabled: bool = True,
    status: str = "running",
) -> dict[str, object]:
    return {
        "id": schedule_id,
        "name": name,
        "description": "nightly digest",
        "command": "python schedule.py",
        "enabled": enabled,
        "status": status,
        "last_error": None,
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z",
        "script": "print('hi')\n",
    }


def test_ls_renders_rows(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    seen = wire.respond(
        "get", [_row(7, "nightly"), _row(12, "hourly", enabled=False, status="stopped")]
    )
    assert wire.run("schedules", "ls") == 0
    assert seen["url"] == f"{wire.url}/api/schedules"
    out = capsys.readouterr().out
    assert "nightly" in out and "running" in out
    assert "hourly" in out and "stopped" in out
    assert "no" in out


def test_ls_empty(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    wire.respond("get", [])
    assert wire.run("schedules", "ls") == 0
    assert "(no schedules)" in capsys.readouterr().out


def test_get_by_numeric_id_skips_the_name_lookup(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("get", _row(7))
    assert wire.run("schedules", "get", "7") == 0
    assert seen["url"] == f"{wire.url}/api/schedules/7"
    out = capsys.readouterr().out
    assert "print('hi')" in out


def test_get_by_name_resolves_via_list(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.sequence("GET", [[_row(7, "nightly")], _row(7)])
    assert wire.run("schedules", "get", "nightly") == 0
    assert [r["url"] for r in wire.requests] == [
        f"{wire.url}/api/schedules",
        f"{wire.url}/api/schedules/7",
    ]


def test_unknown_name_fails_without_a_write(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond("get", [_row(7, "nightly")])
    posted = wire.respond("post", {})
    assert wire.run("schedules", "start", "nope") == 1
    assert posted == {}
    assert "not found" in capsys.readouterr().err


def test_create_posts_script_from_file(
    wire: PublicGateway, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "s.py"
    script.write_text("print('from file')\n")
    seen = wire.respond("post", _row(7, "nightly"), status=201)
    rc = wire.run(
        "schedules",
        "create",
        "--name",
        "nightly",
        "--script-file",
        str(script),
        "--description",
        "digest",
    )
    assert rc == 0
    assert seen["url"] == f"{wire.url}/api/schedules"
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["script"] == "print('from file')\n"
    assert body["name"] == "nightly"
    assert body["enabled"] is True
    assert "command" not in body


def test_create_disabled_flag(wire: PublicGateway) -> None:
    seen = wire.respond("post", _row(7, enabled=False), status=201)
    rc = wire.run(
        "schedules",
        "create",
        "--name",
        "nightly",
        "--script",
        "print(1)",
        "--command",
        "uv run x.py",
        "--disabled",
    )
    assert rc == 0
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["enabled"] is False
    assert body["command"] == "uv run x.py"


def test_create_requires_exactly_one_script_source(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    posted = wire.respond("post", {})
    assert wire.run(mode="schedules-xor-none") == 1
    assert wire.run(mode="schedules-xor-both") == 1
    assert posted == {}
    assert "exactly one of --script / --script-file" in capsys.readouterr().err


def test_create_surfaces_syntax_error_detail(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond(
        "post", {"detail": "script has a syntax error (line 3): invalid syntax"}, status=400
    )
    assert wire.run("schedules", "create", "--name", "n", "--script", "def (") == 1
    assert "syntax error (line 3)" in capsys.readouterr().err


def test_unclassified_error_prints_the_body_before_raising(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond("post", {"detail": [{"loc": ["body", "name"], "msg": "too long"}]}, 422)
    assert (
        wire.run(
            "schedules",
            "create",
            "--name",
            "n",
            "--script",
            "print(1)",
            expect="http",
            status=422,
            path="/api/schedules",
        )
        == 0
    )
    assert "too long" in capsys.readouterr().err


def test_create_name_clash(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    wire.respond("post", {"detail": "exists"}, status=409)
    assert wire.run("schedules", "create", "--name", "nightly", "--script", "print(1)") == 1
    assert "already exists" in capsys.readouterr().err


def test_update_sends_only_the_passed_fields(wire: PublicGateway) -> None:
    seen = wire.respond("put", _row(7))
    rc = wire.run("schedules", "update", "7", "--command", "uv run x.py")
    assert rc == 0
    assert seen["url"] == f"{wire.url}/api/schedules/7"
    assert seen["json"] == {"command": "uv run x.py"}


def test_update_enable_disable_is_tristate(wire: PublicGateway) -> None:
    seen = wire.respond("put", _row(7))
    wire.run("schedules", "update", "7", "--disable")
    assert seen["json"] == {"enabled": False}
    wire.run("schedules", "update", "7", "--enable")
    assert seen["json"] == {"enabled": True}


def test_update_with_no_fields_is_rejected(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    put = wire.respond("put", {})
    assert wire.run(mode="schedules-update-empty") == 1
    assert put == {}
    assert "at least one of" in capsys.readouterr().err


def test_update_reads_script_from_stdin(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("put", _row(7))
    rc = wire.run("schedules", "update", "7", "--script-file", "-", input="print('piped')\n")
    assert rc == 0
    assert seen["json"] == {"script": "print('piped')\n"}


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_control_verbs_hit_their_subpath(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    seen = wire.respond("post", _row(7))
    assert wire.run("schedules", verb, "7") == 0
    assert seen["url"] == f"{wire.url}/api/schedules/7/{verb}"
    assert seen["json"] is None
    assert wire.requests[-1]["body"] == b""
    assert verb in capsys.readouterr().out


def test_restart_of_a_disabled_schedule_surfaces_the_409(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond(
        "post", {"detail": "schedule is disabled; start it instead of restarting"}, status=409
    )
    assert wire.run("schedules", "restart", "7") == 1
    assert "start it instead" in capsys.readouterr().err


def test_delete_force_skips_the_prompt(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("delete", {"status": "deleted"})
    assert wire.run("schedules", "delete", "7", "--force") == 0
    assert seen["url"] == f"{wire.url}/api/schedules/7"
    assert "deleted schedule #7" in capsys.readouterr().out


def test_delete_declined_at_the_prompt_issues_no_request(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    deleted = wire.respond("delete", {})
    assert wire.run("schedules", "delete", "7", input="n\n") == 0
    assert deleted == {}
    assert wire.requests == []
    assert "cancelled" in capsys.readouterr().out


def test_logs_passes_the_line_count_and_labels_the_source(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("get", {"source": "last_error", "lines": ["Traceback", "  boom"]})
    assert wire.run("schedules", "logs", "7", "--lines", str(50)) == 0
    assert seen["url"] == f"{wire.url}/api/schedules/7/logs"
    assert seen["params"] == {"lines": "50"}
    out = capsys.readouterr().out
    assert "last_error" in out and "boom" in out


def test_logs_none_source_is_explained(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond("get", {"source": "none", "lines": []})
    assert wire.run("schedules", "logs", "7", "--lines", str(200)) == 0
    assert "no output yet" in capsys.readouterr().out


def test_runs_renders_history(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    seen = wire.respond(
        "get",
        [
            {"id": 2, "ran_at": "2026-07-02T01:00:00Z", "ok": True, "agent_id": 91, "note": "ok"},
            {"id": 1, "ran_at": "2026-07-01T01:00:00Z", "ok": None, "agent_id": None, "note": None},
        ],
    )
    assert wire.run("schedules", "runs", "7", "--limit", str(10)) == 0
    assert seen["params"] == {"limit": "10"}
    out = capsys.readouterr().out
    assert "91" in out and "2026-07-02T01:00:00Z" in out


def test_runs_puts_a_crash_traceback_under_its_row(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    note = "crashed: TypeError\nTraceback (most recent call last):\n  File x\nTypeError: bad call"
    wire.respond(
        "get",
        [{"id": 3, "ran_at": "2026-07-03T01:00:00Z", "ok": False, "agent_id": None, "note": note}],
    )
    assert wire.run("schedules", "runs", "7", "--limit", str(10)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].endswith("crashed: TypeError")
    assert lines[2:] == [
        "    Traceback (most recent call last):",
        "      File x",
        "    TypeError: bad call",
    ]


def test_runs_empty(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    wire.respond("get", [])
    assert wire.run("schedules", "runs", "7", "--limit", str(50)) == 0
    assert "(no runs yet)" in capsys.readouterr().out


def test_provision_creates_builtins(
    db_conn: psycopg.Connection,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
) -> None:
    assert _sched.cmd_schedules_provision(database_factory=operator_database) == 0
    out = capsys.readouterr().out
    assert "self-evolution-weekly" in out
    assert "memory-arbiter" in out
    assert "trace-ship-tempo" not in out
    with db_conn.cursor() as cur:
        cur.execute("SELECT name, enabled FROM schedules ORDER BY name")
        rows = dict(cur.fetchall())
    assert rows["self-evolution-weekly"] is True
    assert rows["memory-arbiter"] is True
    assert "trace-ship-tempo" not in rows

    assert _sched.cmd_schedules_provision(database_factory=operator_database) == 0
    assert "(all built-in schedules already present and current)" in capsys.readouterr().out
