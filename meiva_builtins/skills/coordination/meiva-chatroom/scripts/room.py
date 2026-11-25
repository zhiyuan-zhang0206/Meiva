#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import shlex
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chatroom_files as cf
import chatroom_json as cj
import chatroom_render as cr
import chatroom_state as cs
import chatroom_wake as cw

REMOTE_SCRIPT = '"$HOME/.meiva/skills/meiva-chatroom/scripts/room.py"'
SSH_CONNECT_TIMEOUT = 10
_SPEC = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*):(.*)")


class Args(argparse.Namespace):
    command: str
    room: str
    title: str
    host_agent: int
    host_label: str
    members: list[str]
    host_machine: str | None
    force: bool
    new_member: str
    from_start: bool
    sender: str
    label: str | None
    mention: list[int]
    mention_all: bool
    caller: int | None
    plan_only: bool
    text: str | None
    stdin: bool
    file: str | None
    since: int
    tail: int | None
    acks: list[str]


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class World:
    local_machine: Callable[[], str]
    caller_id: Callable[[], int | None]
    now: Callable[[], datetime]
    read_stdin: Callable[[], str]
    run_ssh: Callable[[list[str], str], RunResult]
    send: cw.Sender


class RemoteError(RuntimeError):
    pass


def split_room(spec: str) -> tuple[str | None, str]:
    match = _SPEC.fullmatch(spec)
    if match is None:
        return None, spec
    if not match[2].startswith("/"):
        raise ValueError(f"the path after '{match[1]}:' must be absolute, got {match[2]!r}")
    return match[1], match[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="room.py", description="Append-only roundtable rooms for agents"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    room = argparse.ArgumentParser(add_help=False)
    _ = room.add_argument("--room", required=True, help="path to <slug>.md, or <machine>:<path>")

    init = sub.add_parser("init", parents=[room], help="create a room")
    _ = init.add_argument("--title", required=True)
    _ = init.add_argument("--host-agent", type=int, required=True)
    _ = init.add_argument("--host-label", default="Host")
    _ = init.add_argument(
        "--member", dest="members", action="append", default=[], metavar="ID:LABEL"
    )
    _ = init.add_argument("--host-machine", help="machine holding the room (default: this one)")
    _ = init.add_argument("--force", action="store_true", help="replace an existing room")

    add = sub.add_parser("add-member", parents=[room], help="add a participant")
    _ = add.add_argument("--member", dest="new_member", required=True, metavar="ID:LABEL")
    _ = add.add_argument("--from-start", action="store_true", help="deliver the whole history")

    post = sub.add_parser("post", parents=[room], help="append a message and wake the mentioned")
    _ = post.add_argument("--from", dest="sender", required=True, metavar="AGENT_ID|user")
    _ = post.add_argument("--label")
    _ = post.add_argument("--mention", type=int, action="append", default=[], metavar="ID")
    _ = post.add_argument("--mention-all", action="store_true")
    _ = post.add_argument("--caller", type=int, help="agent running this command (never woken)")
    _ = post.add_argument("--plan-only", action="store_true", help="print the wake plan as JSON")
    body = post.add_mutually_exclusive_group(required=True)
    _ = body.add_argument("--text")
    _ = body.add_argument("--stdin", action="store_true")
    _ = body.add_argument("--file")

    read = sub.add_parser("read", parents=[room], help="print messages; moves no cursor")
    _ = read.add_argument("--since", type=int, default=0, metavar="SEQ")
    _ = read.add_argument("--tail", type=int, metavar="N")

    ack = sub.add_parser(
        "ack-cursors", parents=[room], help="internal: move cursors after wakes were delivered"
    )
    _ = ack.add_argument("--ack", dest="acks", action="append", required=True, metavar="ID:SEQ")

    _ = sub.add_parser("status", parents=[room], help="print roster and cursors")
    return parser


def parse_member(raw: str) -> tuple[int, str]:
    agent, _, label = raw.partition(":")
    if not agent.isdecimal() or not label.strip():
        raise ValueError(f"--member expects <agent_id>:<label>, got {raw!r}")
    return int(agent), label.strip()


def parse_ack(raw: str) -> tuple[int, int]:
    agent, _, seq = raw.partition(":")
    if not agent.isdecimal() or not seq.isdecimal():
        raise ValueError(f"--ack expects <agent_id>:<seq>, got {raw!r}")
    return int(agent), int(seq)


def _stamp(world: World) -> str:
    return world.now().strftime("%Y-%m-%d %H:%M")


def _caller(args: Args, world: World) -> int | None:
    return args.caller if args.caller is not None else world.caller_id()


def _body(args: Args, world: World) -> str:
    if args.text is not None:
        return args.text
    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8")
    return world.read_stdin()


def run_local(args: Args, path: str, world: World) -> str:
    files = cf.RoomFiles.at(path)
    if args.command == "init":
        members = dict(parse_member(m) for m in args.members)
        state = cs.new_state(
            title=args.title,
            host_agent=args.host_agent,
            host_machine=args.host_machine or world.local_machine(),
            host_label=args.host_label,
            members=members,
        )
        cf.init_room(files, state, created=_stamp(world), force=args.force)
        return f"created {state.host_machine}:{files.md}\n"
    if args.command == "add-member":
        agent, label = parse_member(args.new_member)
        state = cf.add_member(files, agent, label, from_start=args.from_start, ts=_stamp(world))
        return f"added #{agent}; seq={state.seq}\n"
    if args.command == "ack-cursors":
        state = cf.ack_cursors(files, dict(parse_ack(a) for a in args.acks))
        return f"acked {len(args.acks)}; seq={state.seq}\n"
    if args.command == "post":
        return _post_local(args, files, world)
    state = cf.load(files)
    if args.command == "read":
        shown = cs.select_entries(state.entries, since=args.since, tail=args.tail)
        return "\n".join(cr.render_entry(e) for e in shown)
    return cr.render_status(state)


def _post_local(args: Args, files: cf.RoomFiles, world: World) -> str:
    outcome = cf.post(
        files,
        cs.PostRequest(
            sender=args.sender,
            label=args.label,
            mentions=tuple(args.mention),
            mention_all=args.mention_all,
            body=_body(args, world),
            ts=_stamp(world),
            caller=_caller(args, world),
        ),
    )
    state = outcome.state
    plan = cw.PostPlan(
        seq=state.seq,
        title=state.title,
        host_agent=state.host_agent,
        host_machine=state.host_machine,
        room_path=str(files.md),
        wakes=outcome.wakes,
    )
    return json.dumps(plan.to_json()) + "\n"


def _remote_init(args: Args, machine: str) -> list[str]:
    cmd = ["--title", args.title, "--host-agent", str(args.host_agent)]
    cmd += ["--host-label", args.host_label, "--host-machine", args.host_machine or machine]
    for member in args.members:
        cmd += ["--member", member]
    return cmd + (["--force"] if args.force else [])


def _remote_post(args: Args, world: World) -> list[str]:
    cmd = ["--from", args.sender, "--stdin", "--plan-only"]
    cmd += ["--label", args.label] if args.label else []
    for mention in args.mention:
        cmd += ["--mention", str(mention)]
    cmd += ["--mention-all"] if args.mention_all else []
    caller = _caller(args, world)
    return cmd + (["--caller", str(caller)] if caller is not None else [])


def _remote_read(args: Args) -> list[str]:
    return ["--since", str(args.since)] + (
        ["--tail", str(args.tail)] if args.tail is not None else []
    )


def remote_argv(args: Args, path: str, machine: str, world: World) -> list[str]:
    head = [args.command, "--room", path]
    if args.command == "init":
        return head + _remote_init(args, machine)
    if args.command == "add-member":
        return head + ["--member", args.new_member] + (["--from-start"] if args.from_start else [])
    if args.command == "post":
        return head + _remote_post(args, world)
    if args.command == "ack-cursors":
        return head + [x for ack in args.acks for x in ("--ack", ack)]
    if args.command == "read":
        return head + _remote_read(args)
    return head


def run_remote(args: Args, machine: str, path: str, world: World) -> str:
    remote = f"python3 {REMOTE_SCRIPT} " + shlex.join(remote_argv(args, path, machine, world))
    stdin = _body(args, world) if args.command == "post" else ""
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}", machine]
    result = world.run_ssh([*ssh, remote], stdin)
    if result.returncode != 0:
        raise RemoteError(f"{machine}: exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def finish_post(plan_json: str, args: Args, world: World) -> tuple[str, int]:
    plan = cw.PostPlan.from_json(cj.as_object(cj.parse_json(plan_json)))
    failed = cw.deliver(plan, world.send)
    delivered = {w.target: w.entries[-1].seq for w in plan.wakes if w.target not in failed}
    receipt: dict[str, object] = {"seq": plan.seq, "woke": list(delivered), "room": plan.room_spec}
    if failed:
        receipt["failed"] = {str(t): err for t, err in failed.items()}
        receipt["missed"] = {
            str(w.target): cw.missed_range(w) for w in plan.wakes if w.target in failed
        }
    if delivered:
        try:
            _ = dispatch(ack_args(args, delivered), world)
        except (ValueError, OSError, RemoteError, KeyError, subprocess.TimeoutExpired) as error:
            receipt["unacked"] = f"{type(error).__name__}: {error}"
    return json.dumps(receipt) + "\n", 1 if failed else 0


def ack_args(args: Args, delivered: dict[int, int]) -> Args:
    ack = Args()
    ack.command = "ack-cursors"
    ack.room = args.room
    ack.acks = [f"{agent}:{seq}" for agent, seq in delivered.items()]
    return ack


def dispatch(args: Args, world: World) -> str:
    machine, path = split_room(args.room)
    if machine is not None and machine != world.local_machine():
        return run_remote(args, machine, path, world)
    return run_local(args, path, world)


def execute(args: Args, world: World) -> tuple[str, int]:
    out = dispatch(args, world)
    if args.command != "post" or args.plan_only:
        return out, 0
    return finish_post(out, args, world)


def main(argv: Sequence[str], world: World) -> int:
    args = build_parser().parse_args(argv, namespace=Args())
    try:
        out, code = execute(args, world)
    except subprocess.TimeoutExpired as error:
        _ = sys.stderr.write(
            f"room.py: ssh timed out after {error.timeout}s; the remote command may already be "
            "committed (a post would be duplicated by a retry), so run `read` or `status` first\n"
        )
        return 2
    except (ValueError, OSError, RemoteError, KeyError) as error:
        _ = sys.stderr.write(f"room.py: {type(error).__name__}: {error}\n")
        return 2
    _ = sys.stdout.write(out)
    return code


def real_world() -> World:
    return World(
        local_machine=_meiva_machine,
        caller_id=_meiva_caller,
        now=lambda: datetime.now().astimezone(),
        read_stdin=sys.stdin.read,
        run_ssh=_run_ssh,
        send=_meiva_send,
    )


def _run_ssh(argv: list[str], stdin: str) -> RunResult:
    done = subprocess.run(
        argv, input=stdin, capture_output=True, text=True, timeout=120, check=False
    )
    return RunResult(done.returncode, done.stdout, done.stderr)


def _meiva_machine() -> str:
    try:
        import meiva

        return meiva.self.MACHINE_SPEC.name
    except (ImportError, AttributeError):
        return socket.gethostname()


def _meiva_caller() -> int | None:
    try:
        import meiva

        return meiva.self.AGENT_ID
    except (ImportError, AttributeError):
        return None


def _meiva_send(agent_id: int, content: str) -> None:
    import meiva

    meiva.agents.send_message(agent_id, content)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], real_world()))
