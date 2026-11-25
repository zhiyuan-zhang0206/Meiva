from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

M = load_skill_script(
    "coordination", "meiva-chatroom", "tests", "chatroom_modules.py"
).load_chatroom_modules()
room = M.room
SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "room.py"
NOW = datetime(2026, 11, 25, 13, 45, tzinfo=UTC)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.ssh: list[tuple[list[str], str]] = []
        self.fail_for: set[int] = set()
        self.ssh_reply: room.RunResult | None = None
        self.ssh_queue: list[room.RunResult] = []
        self.ssh_error: Exception | None = None
        self.fake_home: Path | None = None

    def send(self, agent_id: int, text: str) -> None:
        if agent_id in self.fail_for:
            raise ConnectionError("gateway down")
        self.sent.append((agent_id, text))

    def run_ssh(self, argv: list[str], stdin: str) -> room.RunResult:
        self.ssh.append((argv, stdin))
        if self.ssh_error is not None:
            raise self.ssh_error
        if self.ssh_queue:
            return self.ssh_queue.pop(0)
        if self.ssh_reply is not None:
            return self.ssh_reply
        return self.loopback(argv, stdin)

    def loopback(self, argv: list[str], stdin: str) -> room.RunResult:
        assert self.fake_home is not None
        env = {"PATH": os.environ["PATH"], "HOME": str(self.fake_home)}
        done = subprocess.run(  # noqa: S603 - fixed argv built in this test
            ["bash", "-c", argv[-1]],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return room.RunResult(done.returncode, done.stdout, done.stderr)


def make_world(
    rec: Recorder, *, machine: str = "ubuntu", caller: int | None = None, stdin: str = ""
) -> Any:
    return room.World(
        local_machine=lambda: machine,
        caller_id=lambda: caller,
        now=lambda: NOW,
        read_stdin=lambda: stdin,
        run_ssh=rec.run_ssh,
        send=rec.send,
    )


def run(
    argv: list[str], rec: Recorder, capsys: pytest.CaptureFixture[str], **world: Any
) -> tuple[int, str]:
    code = room.main(argv, make_world(rec, **world))
    return code, capsys.readouterr().out


@pytest.fixture
def md(tmp_path: Path) -> Path:
    return tmp_path / "rooms" / "othello.md"


def init(md: Path, rec: Recorder, capsys: pytest.CaptureFixture[str], *extra: str) -> None:
    argv = [
        "init",
        "--room",
        str(md),
        "--title",
        "Othello",
        "--host-agent",
        "6942",
        "--host-label",
        "Host",
        "--member",
        "8901:Claude",
        "--member",
        "8902:GPT",
        *extra,
    ]
    assert run(argv, rec, capsys)[0] == 0


def test_init_writes_a_clean_room_and_hides_its_state(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init(md, Recorder(), capsys)
    assert [p.name for p in md.parent.iterdir() if not p.name.startswith(".")] == ["othello.md"]
    text = md.read_text()
    assert text.startswith("# Othello\n") and "| #8901 | Claude | #000 |" in text
    state = json.loads((md.parent / ".state" / "othello.json").read_text())
    assert state["host_agent"] == 6942 and state["host_machine"] == "ubuntu" and state["seq"] == 0
    assert state["cursors"] == {"6942": 0, "8901": 0, "8902": 0}


def test_init_refuses_to_overwrite_unless_forced(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    again = ["init", "--room", str(md), "--title", "Other", "--host-agent", "1"]
    assert run(again, rec, capsys)[0] == 2
    assert md.read_text().startswith("# Othello")
    assert run([*again, "--force"], rec, capsys)[0] == 0
    assert md.read_text().startswith("# Other")


def test_post_appends_without_touching_history_and_wakes_with_the_delta(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    code, out = run(
        ["post", "--room", str(md), "--from", "8901", "--mention", "8902", "--text", "first claim"],
        rec,
        capsys,
    )
    assert code == 0
    assert json.loads(out) == {"seq": 1, "woke": [8902], "room": f"ubuntu:{md}"}
    before = md.read_text()
    run(
        ["post", "--room", str(md), "--from", "8902", "--mention", "8901", "--text", "rebuttal"],
        rec,
        capsys,
    )
    after = md.read_text()
    assert after.startswith(before)
    assert "### #002 · 2026-11-25 13:45 · GPT (#8902) -> @8901" in after

    (first_target, first_wake), (second_target, second_wake) = rec.sent
    assert (first_target, second_target) == (8902, 8901)
    assert "first claim" in first_wake and f"Room: ubuntu:{md}" in first_wake
    assert "--from 8902 --mention 8901" in first_wake and "room.py post --room" in first_wake
    assert "--stdin <<'CHATROOM_EOF'\n<your reply>\nCHATROOM_EOF\n" in first_wake
    assert '--text "' not in first_wake
    assert "#002..#002" in second_wake and "rebuttal" in second_wake
    assert "first claim" not in second_wake


def test_post_body_from_stdin_and_file(
    md: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    run(
        ["post", "--room", str(md), "--from", "8901", "--stdin"],
        rec,
        capsys,
        stdin='multi\nline `code` "quoted"\n',
    )
    note = tmp_path / "note.txt"
    note.write_text("from a file", encoding="utf-8")
    run(["post", "--room", str(md), "--from", "8901", "--file", str(note)], rec, capsys)
    text = md.read_text()
    assert 'multi\nline `code` "quoted"\n' in text and "from a file" in text
    assert rec.sent == []


def test_user_post_mention_all_skips_the_caller_but_wakes_everyone_else(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    code, out = run(
        ["post", "--room", str(md), "--from", "user", "--mention-all", "--text", "what next?"],
        rec,
        capsys,
        caller=6942,
    )
    assert code == 0 and json.loads(out)["woke"] == [8901, 8902]
    assert "· User -> @all" in md.read_text()
    assert sorted(t for t, _ in rec.sent) == [8901, 8902]


def test_add_member_default_and_from_start(md: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    run(
        ["post", "--room", str(md), "--from", "8901", "--mention", "8902", "--text", "history"],
        rec,
        capsys,
    )
    run(["add-member", "--room", str(md), "--member", "8903:Gemini"], rec, capsys)
    run(["add-member", "--room", str(md), "--member", "8904:Grok", "--from-start"], rec, capsys)
    assert "Gemini (#8903) joined (reads from #002)" in md.read_text()
    rec.sent.clear()
    run(
        [
            "post",
            "--room",
            str(md),
            "--from",
            "8901",
            "--mention",
            "8903",
            "--mention",
            "8904",
            "--text",
            "welcome",
        ],
        rec,
        capsys,
    )
    wakes = dict(rec.sent)
    assert "history" not in wakes[8903] and "welcome" in wakes[8903]
    assert "history" in wakes[8904] and "welcome" in wakes[8904]


def test_read_and_status_never_move_cursors(md: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    for n in range(3):
        run(
            ["post", "--room", str(md), "--from", "8901", "--mention", "8902", "--text", f"m{n}"],
            rec,
            capsys,
        )
    state_file = md.parent / ".state" / "othello.json"
    before = state_file.read_text()
    _, out = run(["read", "--room", str(md), "--since", "1"], rec, capsys)
    assert "m1" in out and "m2" in out and "m0" not in out
    _, out = run(["read", "--room", str(md), "--tail", "1"], rec, capsys)
    assert "m2" in out and "m1" not in out
    _, status = run(["status", "--room", str(md)], rec, capsys)
    assert (
        "seq: 3" in status
        and "#8902 GPT joined_seq=0 cursor=3" in status
        and "#6942 Host joined_seq=0 cursor=0" in status
    )
    assert state_file.read_text() == before


def test_a_failed_wake_is_reported_but_the_message_is_kept(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    rec.fail_for = {8902}
    code, out = run(
        [
            "post",
            "--room",
            str(md),
            "--from",
            "8901",
            "--mention",
            "8902",
            "--mention",
            "6942",
            "--text",
            "kept",
        ],
        rec,
        capsys,
    )
    receipt = json.loads(out)
    assert (
        code == 1 and receipt["woke"] == [6942] and "ConnectionError" in receipt["failed"]["8902"]
    )
    assert "kept" in md.read_text()


@pytest.mark.parametrize(
    "argv",
    [
        ["post", "--from", "9999", "--text", "x"],
        ["post", "--from", "8901", "--mention", "9999", "--text", "x"],
        ["post", "--from", "8901", "--text", "   "],
        ["read"],
    ],
)
def test_invalid_requests_exit_2_and_change_nothing(
    md: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    rec = Recorder()
    if argv[0] == "read":
        assert run([*argv, "--room", str(md)], rec, capsys)[0] == 2
        return
    init(md, rec, capsys)
    before = md.read_text()
    assert run([argv[0], "--room", str(md), *argv[1:]], rec, capsys)[0] == 2
    assert md.read_text() == before and rec.sent == []


def test_concurrent_processes_append_without_losing_or_duplicating_a_message(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init(md, Recorder(), capsys)
    total = 24

    def post(n: int) -> int:
        done = subprocess.run(  # noqa: S603 - fixed argv built in this test
            [
                sys.executable,
                "-P",
                str(SCRIPT),
                "post",
                "--room",
                str(md),
                "--from",
                "8901",
                "--mention",
                "8902",
                "--plan-only",
                "--caller",
                "1",
                "--text",
                f"msg-{n}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(done.stdout)["seq"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = sorted(pool.map(post, range(total)))
    assert seqs == list(range(1, total + 1))
    text = md.read_text()
    assert [text.count(f"### #{s:03d} ") for s in seqs] == [1] * total
    assert all(f"msg-{n}\n" in text for n in range(total))
    assert json.loads((md.parent / ".state" / "othello.json").read_text())["seq"] == total


def test_split_room_separates_machine_from_path() -> None:
    assert room.split_room("/a/b/x.md") == (None, "/a/b/x.md")
    assert room.split_room("rooms/x.md") == (None, "rooms/x.md")
    assert room.split_room("macmini:/a/b/x.md") == ("macmini", "/a/b/x.md")
    with pytest.raises(ValueError, match="absolute"):
        room.split_room("macmini:rooms/x.md")


def test_a_room_on_this_machines_name_is_local(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    code, _ = run(["status", "--room", f"ubuntu:{md}"], rec, capsys)
    assert code == 0 and rec.ssh == []


def test_remote_post_sends_the_body_over_stdin_and_wakes_from_here(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rec = Recorder()
    plan = {
        "seq": 5,
        "title": "Othello",
        "host_agent": 6942,
        "host_machine": "macmini",
        "room_path": "/r/o.md",
        "wakes": [
            {
                "target": 8902,
                "entries": [
                    {
                        "seq": 5,
                        "ts": "t",
                        "sender": "8901",
                        "label": "Claude",
                        "mentions": [8902],
                        "mention_all": False,
                        "body": 'it\'s "quoted"',
                    }
                ],
            }
        ],
    }
    rec.ssh_reply = room.RunResult(0, json.dumps(plan), "")
    body = 'it\'s "quoted"\nsecond $line `x`'
    code, out = run(
        [
            "post",
            "--room",
            "macmini:/r/o.md",
            "--from",
            "8901",
            "--mention",
            "8902",
            "--text",
            body,
        ],
        rec,
        capsys,
        caller=8901,
    )
    assert code == 0 and json.loads(out) == {"seq": 5, "woke": [8902], "room": "macmini:/r/o.md"}
    (argv, stdin), (ack_argv, ack_stdin) = rec.ssh
    assert ack_argv[-1].endswith("ack-cursors --room /r/o.md --ack 8902:5") and ack_stdin == ""
    assert argv[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "macmini"]
    assert argv[6] == (
        'python3 "$HOME/.meiva/skills/meiva-chatroom/scripts/room.py" post --room /r/o.md '
        "--from 8901 --stdin --plan-only --mention 8902 --caller 8901"
    )
    assert stdin == body
    ((target, text),) = rec.sent
    assert (
        target == 8902
        and "Room: macmini:/r/o.md" in text
        and "--room macmini:/r/o.md --from 8902 --mention 8901" in text
    )


def test_remote_failure_surfaces_the_ssh_error(capsys: pytest.CaptureFixture[str]) -> None:
    rec = Recorder()
    rec.ssh_reply = room.RunResult(255, "", "Connection refused")
    code = room.main(["status", "--room", "macmini:/r/o.md"], make_world(rec))
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert "macmini: exit 255: Connection refused" in captured.err


def test_remote_room_end_to_end_through_a_loopback_ssh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    rec.fake_home = tmp_path / "home"
    skills = rec.fake_home / ".meiva" / "skills"
    skills.mkdir(parents=True)
    (skills / "meiva-chatroom").symlink_to(SKILL_DIR)
    md = tmp_path / "ws" / "rooms" / "topic.md"
    spec = f"ubuntu:{md}"
    client = {"machine": "macbook", "caller": 8903}

    code, out = run(
        [
            "init",
            "--room",
            spec,
            "--title",
            "Topic",
            "--host-agent",
            "6942",
            "--member",
            "8903:Gemini",
            "--member",
            "8901:Claude",
        ],
        rec,
        capsys,
        **client,
    )
    assert code == 0, out
    assert json.loads((md.parent / ".state" / "topic.json").read_text())["host_machine"] == "ubuntu"

    code, out = run(
        [
            "post",
            "--room",
            spec,
            "--from",
            "8903",
            "--mention",
            "8901",
            "--text",
            "across machines",
        ],
        rec,
        capsys,
        **client,
    )
    assert code == 0 and json.loads(out) == {"seq": 1, "woke": [8901], "room": spec}
    assert "across machines" in md.read_text()
    ((target, wake),) = rec.sent
    assert target == 8901 and f"Room: {spec}" in wake and "across machines" in wake

    _, status = run(["status", "--room", spec], rec, capsys, **client)
    assert "seq: 1" in status and "#8901 Claude joined_seq=0 cursor=1" in status
    _, read = run(["read", "--room", spec, "--since", "0"], rec, capsys, **client)
    assert "across machines" in read
    assert (
        run(["add-member", "--room", spec, "--member", "8904:Grok"], rec, capsys, **client)[0] == 0
    )


def post_argv(md: str, sender: str, *mentions: int, text: str) -> list[str]:
    argv = ["post", "--room", md, "--from", sender, "--text", text]
    for mention in mentions:
        argv += ["--mention", str(mention)]
    return argv


def cursors(md: Path, rec: Recorder, capsys: pytest.CaptureFixture[str]) -> dict[int, int]:
    state = json.loads((md.parent / ".state" / f"{md.stem}.json").read_text())
    return {int(i): c for i, c in state["cursors"].items()}


def test_a_failed_wake_keeps_that_targets_cursor_so_the_next_wake_resends(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    rec.fail_for = {8902}
    code, out = run(post_argv(str(md), "8901", 8902, 6942, text="one"), rec, capsys)
    receipt = json.loads(out)
    assert code == 1 and receipt["woke"] == [6942] and receipt["missed"] == {"8902": [1, 1]}
    assert cursors(md, rec, capsys) == {6942: 1, 8901: 1, 8902: 0}

    rec.fail_for = set()
    rec.sent.clear()
    code, out = run(post_argv(str(md), "8901", 8902, text="two"), rec, capsys)
    assert code == 0 and json.loads(out)["woke"] == [8902]
    ((_, wake),) = rec.sent
    assert "Unread for you: #001..#002" in wake and "one" in wake and "two" in wake
    assert cursors(md, rec, capsys)[8902] == 2


def test_a_failed_wake_keeps_the_cursor_over_a_remote_room_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    rec.fake_home = tmp_path / "home"
    skills = rec.fake_home / ".meiva" / "skills"
    skills.mkdir(parents=True)
    (skills / "meiva-chatroom").symlink_to(SKILL_DIR)
    md = tmp_path / "ws" / "rooms" / "topic.md"
    spec = f"ubuntu:{md}"
    client = {"machine": "macbook", "caller": 8903}
    init_argv = ["init", "--room", spec, "--title", "T", "--host-agent", "6942"]
    members = ["--member", "8903:Gemini", "--member", "8901:Claude", "--member", "8902:GPT"]
    assert run([*init_argv, *members], rec, capsys, **client)[0] == 0

    rec.fail_for = {8902}
    code, out = run(post_argv(spec, "8903", 8901, 8902, text="one"), rec, capsys, **client)
    receipt = json.loads(out)
    assert code == 1 and receipt["woke"] == [8901] and receipt["missed"] == {"8902": [1, 1]}
    assert "unacked" not in receipt
    assert cursors(md, rec, capsys) == {6942: 0, 8903: 1, 8901: 1, 8902: 0}
    _, status = run(["status", "--room", spec], rec, capsys, **client)
    assert "#8902 GPT joined_seq=0 cursor=0" in status

    rec.fail_for = set()
    rec.sent.clear()
    code, _ = run(post_argv(spec, "8903", 8902, text="two"), rec, capsys, **client)
    assert code == 0
    ((target, wake),) = rec.sent
    assert target == 8902 and "Unread for you: #001..#002" in wake
    assert cursors(md, rec, capsys)[8902] == 2


def test_a_failed_ack_is_reported_without_failing_the_post(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    spec = "macmini:/r/o.md"
    plan = {
        "seq": 1,
        "title": "T",
        "host_agent": 1,
        "host_machine": "macmini",
        "room_path": "/r/o.md",
        "wakes": [
            {
                "target": 8902,
                "entries": [
                    {
                        "seq": 1,
                        "ts": "t",
                        "sender": "8901",
                        "label": "C",
                        "mentions": [8902],
                        "mention_all": False,
                        "body": "x",
                    }
                ],
            }
        ],
    }
    rec.ssh_queue = [room.RunResult(0, json.dumps(plan), ""), room.RunResult(255, "", "gone")]
    code, out = run(post_argv(spec, "8901", 8902, text="x"), rec, capsys)
    receipt = json.loads(out)
    assert code == 0 and receipt["woke"] == [8902] and "gone" in receipt["unacked"]


def test_an_ssh_timeout_says_the_remote_command_may_have_committed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rec = Recorder()
    rec.ssh_error = subprocess.TimeoutExpired(["ssh"], 120)
    code = room.main(post_argv("macmini:/r/o.md", "8901", 8902, text="x"), make_world(rec))
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert "timed out after 120s" in captured.err and "may already be committed" in captured.err
    assert "Traceback" not in captured.err


def test_an_unclosed_fence_in_a_post_does_not_swallow_the_next_entry(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    run(post_argv(str(md), "8901", text="code:\n~~~\nx = 1"), rec, capsys)
    run(post_argv(str(md), "8902", text="after"), rec, capsys)
    text = md.read_text()
    assert "x = 1\n~~~\n\n---\n### #002" in text
    assert "after\n" in text and "first" not in text


def test_ack_cursors_command_rejects_bad_input(
    md: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()
    init(md, rec, capsys)
    for ack in ("9999:0", "8901:5", "8901"):
        assert run(["ack-cursors", "--room", str(md), "--ack", ack], rec, capsys)[0] == 2
    assert run(["ack-cursors", "--room", str(md), "--ack", "8901:0"], rec, capsys)[0] == 0
