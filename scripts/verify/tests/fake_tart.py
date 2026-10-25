from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

HOME = Path(__file__).resolve().parent


def _load() -> dict[str, Any]:
    return json.loads((HOME / "state.json").read_text())


def _save(state: dict[str, Any]) -> None:
    (HOME / "state.json").write_text(json.dumps(state))


def _record(argv: list[str], stdin: str | None) -> None:
    with (HOME / "calls.log").open("a") as log:
        log.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")


def _exec(state: dict[str, Any], rest: list[str]) -> int:
    name = rest[0]
    if not state["vms"].get(name, {}).get("running"):
        return 1
    if rest[1:] == ["true"]:
        return 0
    script = rest[rest.index("-c") + 1]
    if any(marker in script for marker in state["fail_on"]):
        print("fake tart: scripted failure", file=sys.stderr)
        return 1
    if "cat $HOME/verify/observer.json" in script:
        print(state["observer"])
    elif "base64" in script:
        print(state["logs_b64"])
    return 0


def _list(state: dict[str, Any]) -> int:
    if state.get("fail_list"):
        return 1
    vms: dict[str, dict[str, bool]] = state["vms"]
    rows = [{"Source": "local", "Name": n, "Running": v["running"]} for n, v in vms.items()]
    rows.append({"Source": "OCI", "Name": "ghcr.io/example/image@sha256:0", "Running": False})
    print(json.dumps(rows))
    return 0


def _delete(state: dict[str, Any], name: str) -> int:
    vms: dict[str, dict[str, bool]] = state["vms"]
    if vms[name]["running"]:
        return 1
    if state.get("fail_delete"):
        state["fail_list"] = True
        _save(state)
        return 1
    if not state.get("keep_on_delete"):
        del vms[name]
        _save(state)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    stdin = sys.stdin.read() if argv[:2] == ["exec", "-i"] else None
    _record(argv, stdin)
    state = _load()
    vms: dict[str, dict[str, bool]] = state["vms"]
    command = argv[0]
    if command == "--version":
        print("9.9.9-fake")
    elif command == "list":
        return _list(state)
    elif command == "clone":
        if argv[1] not in vms:
            return 1
        vms[argv[2]] = {"running": False}
        _save(state)
    elif command == "run":
        vms[argv[-1]]["running"] = True
        _save(state)
        while _load()["vms"].get(argv[-1], {}).get("running"):
            time.sleep(0.05)
    elif command == "stop":
        vms[argv[1]]["running"] = False
        _save(state)
    elif command == "delete":
        return _delete(state, argv[1])
    elif command == "exec":
        return _exec(state, argv[1:] if stdin is None else argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
