# meiva:note
# The line a background session runs, the launch file that carries it, and which logs the ring evicts.
from __future__ import annotations

from pathlib import Path

import pytest

from meiva.shell import background_plan, log_ring
from meiva.shell.background_plan import Notice, NotifyPolicy, notified_line, notify_policy


def _notice(
    *, label: str = "Background command 'build'", cli: Path = Path("/venv/bin/meiva")
) -> Notice:
    return Notice(agent_id=5, label=label, source="shell:3", cli=cli)


def test_notified_line_structure(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    line = notified_line("make build", _notice(), output_path=log, keep=False)
    # meiva:note
    # Subshell so the pipeline covers a compound cmd; command exit code pinned
    # before anything else runs; the shell waits for the exit marker to land in
    # the log before the notice, so a session close cannot eat the tee's buffered
    # tail; CLI notice with source + tail; close after delivery.
    assert line.startswith(
        f"exec 3> >(tee {log}); ( make build ) >&3 2>&1; _ec=$?; "
        f"printf '[exit %s]\\n' \"$_ec\" >&3; exec 3>&-; _polls=0; while "
    )
    assert f"$(tail -c {background_plan.EXIT_TAIL_BYTES} {log} 2>/dev/null)" in line
    assert "break;; esac; sleep " in line
    assert line.index("_polls=0") < line.index("agents send 5")
    assert "/venv/bin/meiva agents send 5" in line
    assert "Background command 'build' finished. Full output at" in line
    assert "exited with code" not in line
    assert "--source shell:3" in line
    assert f"--tail-file {log}" in line
    assert "--completion" in line
    assert line.endswith("; exit $_ec")


def test_a_retained_session_is_not_closed_and_does_not_wait_for_the_log(tmp_path: Path) -> None:
    line = notified_line("sleep 1", _notice(), output_path=tmp_path / "x.log", keep=True)
    assert not line.endswith("; exit $_ec")
    assert "agents send 5" in line
    # meiva:note
    # A retained session is not closing, so the tee cannot be killed under the
    # notice: the notice still fires at command exit, as documented.
    assert "_polls" not in line


@pytest.mark.parametrize(
    ("notify", "expected_fragment"),
    [(None, "--completion"), (NotifyPolicy.ALWAYS, "agents send 5")],
)
def test_the_notice_follows_the_notify_policy(
    tmp_path: Path, notify: NotifyPolicy | None, expected_fragment: str
) -> None:
    line = notified_line(
        "make build", _notice(), output_path=tmp_path / "x.log", keep=False, notify=notify
    )
    assert expected_fragment in line
    assert ("--completion" in line) is (notify is None)
    assert 'if [ "$_ec"' not in line


def test_the_cli_path_is_quoted_into_the_line(tmp_path: Path) -> None:
    line = notified_line(
        "true", _notice(cli=Path("/opt/my tools/meiva")), output_path=tmp_path / "x.log", keep=True
    )
    assert "'/opt/my tools/meiva' agents send 5" in line


def test_each_known_notify_text_maps_to_its_policy() -> None:
    assert notify_policy(None) is None
    assert notify_policy("always") is NotifyPolicy.ALWAYS
    assert [policy.value for policy in NotifyPolicy] == ["always"]


@pytest.mark.parametrize("text", ["failure", "success", "Always", "", " always"])
def test_an_unknown_notify_text_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r"notify must be one of \['always'\], got "):
        notify_policy(text)


def test_a_line_refuses_a_multiline_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="single line"):
        notified_line("a\nb", _notice(), output_path=tmp_path / "x.log", keep=False)


def test_a_label_cannot_reopen_shell_expansion(tmp_path: Path) -> None:
    # meiva:note
    # The label rides inside the double-quoted notice where only ${_ec} may expand; shell-active characters must be rejected here, not just by callers.
    with pytest.raises(ValueError, match="shell-active"):
        notified_line(
            "echo hi",
            _notice(label='x"; rm -rf ~; echo "'),
            output_path=tmp_path / "x.log",
            keep=False,
        )


def test_the_launch_file_removes_itself_then_runs_the_line(tmp_path: Path) -> None:
    path = tmp_path / "7_build.sh"
    assert (
        background_plan.launch_file_text("echo hi", path) == f"command rm -f -- {path}\necho hi\n"
    )
    assert background_plan.source_command(path) == f". {path}"


def test_a_path_with_a_space_is_quoted_in_both_halves() -> None:
    path = Path("/work space/7_build.sh")
    assert background_plan.launch_file_text("x", path).startswith("command rm -f -- '/work space/")
    assert background_plan.source_command(path) == ". '/work space/7_build.sh'"


def _logs(*entries: tuple[str, float]) -> list[log_ring.LogFile]:
    return [log_ring.LogFile(name, modified) for name, modified in entries]


def test_logs_live_in_the_workspace_by_session_id_and_name() -> None:
    workspace = Path("/ws/4")
    assert log_ring.output_dir(workspace) == workspace / ".shell_logs"
    assert log_ring.log_path(workspace / ".shell_logs", 12, "build") == (
        workspace / ".shell_logs" / "12_build.log"
    )


def test_a_ring_within_its_size_evicts_nothing() -> None:
    logs = _logs(*((f"{i}_seed.log", float(i)) for i in range(log_ring.OUTPUT_KEEP)))
    assert log_ring.evictions(logs, live=frozenset()) == []


def test_the_oldest_logs_beyond_the_size_are_evicted_first() -> None:
    logs = _logs(*((f"{i}_seed.log", float(i)) for i in range(log_ring.OUTPUT_KEEP + 3)))
    assert log_ring.evictions(logs, live=frozenset()) == ["0_seed.log", "1_seed.log", "2_seed.log"]


def test_a_live_sessions_log_outranks_the_ring_cap() -> None:
    # meiva:note
    # A long-lived watcher's log keeps its spawn-time mtime forever (a time watcher writes nothing to stdout), so mtime-LRU alone would evict it while the session still runs.
    logs = _logs(("7_cron.log", -100.0), *((f"{100 + i}_seed.log", float(i)) for i in range(25)))
    victims = log_ring.evictions(logs, live={7})
    assert "7_cron.log" not in victims
    assert victims == [f"{100 + i}_seed.log" for i in range(5)]
    assert log_ring.evictions(logs, live=set())[0] == "7_cron.log"


def test_a_name_without_a_numeric_id_is_not_protected_by_a_live_id() -> None:
    logs = _logs(
        ("old.log", 0.0), *((f"{i}_x.log", float(i + 1)) for i in range(log_ring.OUTPUT_KEEP))
    )
    assert log_ring.evictions(logs, live={0, 1, 2}) == ["old.log"]
