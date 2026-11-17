# meiva:note
# Persist and arm a host-owned timer; never replace or unload the executing job.


from __future__ import annotations

import fcntl
import json
import plistlib
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter


def save(path: Path, record: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2, default=str) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def arm(
    path: Path,
    *,
    command: list[str],
    moment: datetime,
    env: Mapping[str, str],
    system: str,
    uid: int,
) -> None:
    label = "com.meiva.scheduled-update." + path.stem
    if system == "Darwin":
        job = {
            "Label": label,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": False,
            "EnvironmentVariables": dict(env),
            "StandardOutPath": str(path.with_suffix(".log")),
            "StandardErrorPath": str(path.with_suffix(".log")),
        }
        plist = path.with_suffix(".plist")
        plist.write_bytes(plistlib.dumps(job))
        plist.chmod(0o600)
        argv = ["launchctl", "bootstrap", f"gui/{uid}", str(plist)]
    elif system == "Linux":
        argv = [
            "systemd-run",
            "--user",
            f"--unit={label}",
            "--collect",
            f"--on-calendar={moment.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]
        argv.extend(f"--setenv={key}={value}" for key, value in env.items())
        argv.extend(["--", *command])
    else:
        raise ValueError(f"unsupported host timer OS: {system}")
    subprocess.run(argv, env=dict(env), check=True, capture_output=True, text=True)


def _manage(path: Path, verb: str, *, env: Mapping[str, str], system: str, uid: int) -> int:
    record = TypeAdapter(dict[str, object]).validate_json(path.read_text())
    if verb == "cancel" and record["state"] == "pending":
        label = "com.meiva.scheduled-update." + path.stem
        argv = (
            ["launchctl", "bootout", f"gui/{uid}/{label}"]
            if system == "Darwin"
            else ["systemctl", "--user", "stop", label + ".timer"]
        )
        subprocess.run(argv, env=dict(env), check=True, capture_output=True, text=True)
        record["state"] = "cancelled"
        save(path, record)
    print(json.dumps(record, indent=2, default=str))
    return 0


def manage(path: Path, verb: str, *, env: Mapping[str, str], system: str, uid: int) -> int:
    if verb == "status":
        print(path.read_text())
        return 0
    with path.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("update is executing; cancellation refused; inspect its log")
            return 2
        return _manage(path, verb, env=env, system=system, uid=uid)
