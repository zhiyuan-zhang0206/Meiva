# meiva:note
# The `ssh` the fleet-update tests put first on the PATH: a program, not a stand-in function. It
# is run once per command the window sends, as a child of the window, so everything between them
# is the real thing -- argv, stdin, the exit status and the lines the window reads back.
#
# On the other side it plays a small cluster. The cluster's state lives in a JSON file the test
# writes before the run and reads after it (`FLEET_CLUSTER_STATE`); this program applies each
# command to that state the way the hosts would, records the effects it accepted, and refuses
# production's forbidden orders at the offending command -- a gateway stop or start while a
# runner is up, a runner start before the gateway, a switch before its stop, a macOS start that
# is not the one-shot LaunchAgent. A refusal is recorded as a violation the test reads back, and
# the command exits non-zero. Standard library only: it runs once per command, so startup is the
# cost.
# meiva:link cli/tests/fleet_update/docs/fleet-update-harness.meiva.okf.md

from __future__ import annotations

import fcntl
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

State = dict[str, Any]


class Sim:
    def __init__(self, state: State) -> None:
        self.s = state

    def _runners_up(self) -> bool:
        return any(h["up"] for a, h in self.s["hosts"].items() if a != "gw")

    def _failure(self, kind: str, alias: str) -> int:
        return int(self.s["fail"].get(f"{kind}|{alias}", 0))

    # meiva:note
    # One stop: a staged failure keeps its daemon only when the test says so.
    def _stop(self, alias: str, host: State, command: str) -> int:
        assert not (alias == "gw" and self._runners_up()), "gateway stopped before a runner"
        if host["stop_message"] == "no":
            assert "--message" not in command and "--no-message" not in command, (
                "legacy host received --message/--no-message"
            )
        rc = int(self.s["stop_fails"].get(alias, 0))
        if not rc or alias not in self.s["stop_fails_leaves_up"]:
            host["up"] = False
            self.s["host_up"][alias] = False
        self.s["journal"][alias] = {
            "operation": "stop",
            "complete": True,
            "result": {"rc": rc, "error": None if not rc else "RuntimeError: helper busy"},
        }
        if alias in self.s["journal_after_stop"]:
            self.s["journal"][alias] = self.s["journal_after_stop"][alias]
        return rc

    def _switch(self, host: State, command: str) -> None:
        assert not host["up"] and self.s["new"] in command, "switched a running host"
        assert self.s["clean"] in shlex.split(command)[2], "no clean-tree check after switch"
        assert "uv sync --frozen --compile-bytecode --reinstall-package meiva" in command
        host["head"] = self.s["new"]

    def _start(self, kind: str, alias: str, host: State) -> int:
        assert (kind == "oneshot") == (host["os"] == "Darwin"), "a macOS start must be a one-shot"
        gateway_ok = self.s["hosts"]["gw"]["up"] if alias != "gw" else not self._runners_up()
        assert gateway_ok, "started a host out of gateway-first order"
        if alias in self.s["recover_fails"]:
            return 1
        host["up"] = True
        self.s["host_up"][alias] = True
        return 0

    # meiva:note
    # A smoke runs on the gateway, so the host it is about is the one in the command: a test
    # stages that machine's red smoke without touching the gateway's own line.
    def _smoke(self, command: str) -> int:
        name = command.partition(" - smoke ")[2].split()[0].strip("'")
        self.s["smoked"].append(name)
        return self._failure("smoke", name)

    def _effect(self, kind: str, alias: str, host: State, command: str) -> int:
        self.s["effects"].append([kind, alias])
        if rc := self._failure(kind, alias):
            return rc
        if kind == "stop":
            return self._stop(alias, host, command)
        if kind == "switch":
            self._switch(host, command)
        elif kind in ("start", "oneshot"):
            return self._start(kind, alias, host)
        elif kind == "smoke":
            return self._smoke(command)
        elif kind == "refresh":
            assert "--force" not in command
        return 0

    def _reported(self, alias: str, host: State, emit: list[str]) -> int:
        for key in ("head", "dirty", "hook", "active", "stop_message"):
            emit.append(f"{key}={host[key]}")
        journal = self.s["journal"].get(alias)
        emit.append(f"op={'' if journal is None else json.dumps(journal)}")
        emit.append(f"root={'up' if host['up'] else 'down'}")
        return 0

    def _roster_json(self) -> str:
        if self.s["roster_reply"] is not None:
            return str(self.s["roster_reply"])
        online = self.s["roster_lag"] <= 0
        self.s["roster_reads"] += 1
        self.s["roster_lag"] -= 1
        keys = (
            "name",
            "online",
            "identity_mismatch",
            "head_sha",
            "running_sha",
            "serve_agent_runner",
        )
        values: list[list[Any]] = [
            [
                self.s["names"][a],
                h["up"] and online and a not in self.s["silent"],
                False,
                h["head"],
                h["head"],
                True,
            ]
            for a, h in self.s["hosts"].items()
            if a not in self.s["hidden"]
        ]
        if self.s["laptop"]:
            values.append(["laptop", *self.s["laptop"]["row"]])
        return json.dumps([dict(zip(keys, row, strict=True)) for row in values])

    def handle(self, alias: str, command: str, stdin: str, emit: list[str]) -> int:
        self.s["commands"].append([alias, command])
        host = self.s["hosts"][alias]
        emit.append(self.s["output"])
        programs = self.s["programs"]
        if command == "uname -s":
            emit.append(str(host["os"]))
            return 0
        if '"head=$(git rev-parse HEAD)"' in command:
            return self._reported(alias, host, emit)
        if "ConfiguredMachineIdentity(ConfigBoot()).name()" in command:
            emit.append(self.s["names"][alias])
            return 0
        if " - roster" in command:
            assert stdin == programs["gateway"], "the roster program is not the shipped source"
            emit.append(self._roster_json())
            return 0
        if stdin in (programs["silence"], programs["leases"]):
            return self._shipped(alias, command, stdin, emit)
        if "agent_host.pid" in command:
            return self._evidence(alias, emit)
        if "helper-signing.py" in command and " facts" in command:
            emit.append(self._helper_facts(host))
            return 0
        if "converge-incomplete.json" in command:
            return self._incomplete(alias, emit)
        if "verify" in command:
            return self._verify(alias, command, emit)
        return self._effect_for(alias, host, command, emit)

    def _evidence(self, alias: str, emit: list[str]) -> int:
        if rc := self._failure("evidence", alias):
            return rc
        emit.append(f"host={'up' if self.s['host_up'][alias] else 'down'}")
        journal = self.s["journal"].get(alias)
        if journal is not None:
            emit.append(json.dumps(journal))
        return 0

    def _effect_for(self, alias: str, host: State, command: str, emit: list[str]) -> int:
        kinds = {
            "git fetch": "fetch",
            'meiva" stop': "stop",
            "--detach": "switch",
            "launchctl bootstrap": "oneshot",
            'meiva" start': "start",
            " - smoke": "smoke",
            "packages refresh": "refresh",
        }
        kind = next(k for s, k in kinds.items() if s in command)
        if kind == "refresh":
            emit.append("  summary: applied 2, conflict 1")
        return self._effect(kind, alias, host, command)

    # meiva:note
    # The helper facts one host reports; a test stages a host by editing its dict, exactly as the
    # owner module would report it, so no assertion needs a real machine.
    def _helper_facts(self, host: State) -> str:
        if error := host.get("probe_error"):
            return f"h_error={error}"
        helper = host.get("helper", "present")
        lines = [f"h_rc={host.get('facts_rc', 0)}", f"h_helper={helper}"]
        if helper == "absent":
            return "\n".join(lines)
        facts = {
            "recorded_hash": host.get("recorded_hash", self.s["helper_hash"]),
            "expected_hash": self.s["helper_hash"],
            "loaded": host.get("loaded", "no"),
            "valid": host.get("valid", "yes"),
            "cert_root": host.get("cert_root", self.s["helper_cert_root"]),
            "signed_at": "2026-10-22T00:00:00+00:00",
        }
        lines += [f"h_{name}={value}" for name, value in facts.items()]
        stale = "no" if facts["recorded_hash"] == facts["expected_hash"] else "yes"
        lines.append(f"h_stale={host.get('stale', stale)}")
        dropped = host.get("drop_fact")
        return "\n".join(
            line for line in lines if dropped is None or not line.startswith(f"h_{dropped}=")
        )

    def _incomplete(self, alias: str, emit: list[str]) -> int:
        recorded = self.s["incomplete"].get(alias)
        if recorded is None:
            return 1
        emit.extend(recorded.splitlines())
        return 0

    # meiva:note
    # A program the window ships to the gateway over stdin: silence, else leases.
    def _shipped(self, alias: str, command: str, stdin: str, emit: list[str]) -> int:
        if stdin == self.s["programs"]["silence"]:
            return self._silence(alias, command, emit)
        self.s["lease_reads"].append(len(self.s["effects"]))
        assert alias == "gw", "the lease table is read on the gateway host"
        reply = self.s["lease_reply"]
        emit.append(reply if reply is not None else json.dumps({"leases": self.s["leases"]}))
        return int(self.s["lease_rc"])

    def _silence(self, alias: str, command: str, emit: list[str]) -> int:
        assert alias == "gw", "the silence is Grafana's: it runs on the gateway host"
        script = shlex.split(command)[2]
        verb, *rest = shlex.split(script.partition('/bin/python" - ')[2])
        self.s["effects"].append([f"silence-{verb}", alias])
        if self.s["silence_reply"] is not None:
            emit.append(self.s["silence_reply"])
            return 0
        if verb == "open":
            self.s["silence_hours"] = float(rest[rest.index("--hours") + 1])
            self.s["silence_comment"] = rest[rest.index("--comment") + 1]
            self.s["silence_open"] = True
            emit.append("SILENCE opened id=s1 until=later")
        else:
            self.s["silence_open"] = False
            emit.append("SILENCE closed 1")
        return 0

    def _verify(self, alias: str, command: str, emit: list[str]) -> int:
        kind = (
            "pre-verify"
            if "pre-update-verify" in command
            else "schedules-verify"
            if "schedules verify" in command
            else "plugins-verify"
        )
        self.s["effects"].append([kind, alias])
        if kind == "pre-verify":
            assert alias == "gw" and self.s["new"] in command
            assert command.index("_read_schedule_rows") < command.index("worktree add")
            assert command.index("worktree add") < command.index("cli.main")
            fresh = [k for k, _ in self.s["effects"][self.s["run_started_at"] :]]
            assert "stop" not in fresh, "checked after a service stopped"
            code, lines = self.s["stored_schedules"]
            for line in [*lines, f"VERIFY_RC={code}"]:
                emit.append(line)
            return 0
        assert kind != "schedules-verify" or "--no-notify" in command
        if rc := self._failure(kind, alias):
            emit.append(f"RED the {kind} detail from {alias}")
        return rc


def _barrier_participant(alias: str, command: str) -> tuple[str, str] | None:
    if 'meiva" stop' in command:
        return "stop", alias
    if "--detach" in command:
        return "switch", alias
    if "launchctl bootstrap" in command or 'meiva" start' in command:
        return "start", alias
    if " - smoke " in command:
        return "smoke", command.partition(" - smoke ")[2].split()[0].strip("'")
    if "packages refresh" in command:
        return "refresh", alias
    return None


def _wait_barrier(path: Path, alias: str, command: str) -> str | None:
    matched = _barrier_participant(alias, command)
    if matched is None:
        return None
    wave, participant = matched
    with path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with path.open(encoding="utf-8") as handle:
            barriers: dict[str, list[str]] = json.load(handle).get("barriers", {})
    expected = barriers.get(wave)
    if not expected or participant not in expected:
        return None
    barrier_dir = path.parent / f"barrier-{wave}"
    barrier_dir.mkdir(exist_ok=True)
    (barrier_dir / participant).write_text("arrived", encoding="utf-8")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if all((barrier_dir / member).exists() for member in expected):
            return None
        time.sleep(0.005)
    missing = [member for member in expected if not (barrier_dir / member).exists()]
    return f"{wave} wave did not run concurrently; {participant} waited on {missing}"


def main(argv: list[str]) -> int:
    alias, command = argv[-2], argv[-1]
    path = Path(os.environ["FLEET_CLUSTER_STATE"])
    stdin = sys.stdin.buffer.read().decode("utf-8")
    barrier_violation = _wait_barrier(path, alias, command)
    emitted: list[str] = []
    with path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with path.open(encoding="utf-8") as handle:
            state: State = json.load(handle)
        if barrier_violation is not None:
            state["violations"].append(f"{alias}: {barrier_violation}")
            emitted.append(f"VIOLATION {barrier_violation}")
            rc = 99
        else:
            try:
                rc = Sim(state).handle(alias, command, stdin, emitted)
            except AssertionError as violation:
                state["violations"].append(f"{alias}: {violation}")
                emitted.append(f"VIOLATION {violation}")
                rc = 99
        with path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle)
    sys.stdout.buffer.write("".join(f"{line}\n" for line in emitted).encode("utf-8"))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
