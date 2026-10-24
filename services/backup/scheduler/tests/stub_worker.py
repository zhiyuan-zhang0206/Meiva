# meiva:note
# A scripted operation worker for the controller tests: it does what its mode says.
#
# `run_operation` launches a worker as `python -I -B worker_bootstrap.py <code root> <module>
# request.json result.json`; a test names this module and picks the mode through the request
# (a direct `run_operation`) or the `STUB_MODE` environment variable (a `run_job` whose
# request is fixed). Every mode is a real child process doing real file work, so the
# controller's launch, wait, stop and cleanup are the production code.

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from services.backup.scheduler.operation.worker_process import worker_request

_MODES = (
    "succeed",
    "ok",
    "exit",
    "exit-and-remove-work",
    "hang",
    "plaintext-then-hang",
    "scratch-then-hang",
    "dump",
    "restore",
    "fail",
)


def _log(mode: str) -> None:
    log = os.environ.get("STUB_LOG")
    if log:
        with Path(log).open("a", encoding="utf-8") as handle:
            handle.write(f"{mode}\n")


def main() -> None:
    request, result = worker_request(sys.argv)
    work = result.parent
    mode = str(request.get("mode") or os.environ["STUB_MODE"])
    if mode not in _MODES:
        raise SystemExit(f"unknown stub mode {mode!r}")
    if mode == "succeed":
        mode = str(request["kind"])
    _log(mode)
    if mode == "ok":
        result.write_text('{"ok":true}')
    elif mode == "exit":
        payload = request.get("payload")
        if isinstance(payload, str):
            result.write_text(payload)
        raise SystemExit(int(str(request["code"])))
    elif mode == "exit-and-remove-work":
        shutil.rmtree(work)
        raise SystemExit(3)
    elif mode == "hang":
        time.sleep(60)
    elif mode == "plaintext-then-hang":
        plaintext = Path(str(request["plaintext"]))
        plaintext.write_text("private")
        Path(str(request["ready"])).touch()
        try:
            time.sleep(60)
        finally:
            plaintext.unlink()
    elif mode == "scratch-then-hang":
        scratch = work / "scratch"
        scratch.mkdir()
        (scratch / "plaintext").write_text("private")
        Path(str(request["ready"])).touch()
        time.sleep(60)
    elif mode == "dump":
        stamp = datetime.fromisoformat(str(request["now"])).strftime("%Y%m%dT%H%M%SZ")
        artifact = work / "artifact" / f"stubdb-{stamp}.dump.enc"
        artifact.parent.mkdir()
        artifact.write_bytes(b"encrypted " + stamp.encode())
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        result.write_text(json.dumps({"artifact": artifact.name, "sha256": digest}))
    elif mode == "restore":
        result.write_text('{"restored":true}')
    else:
        sys.stderr.write("scripted failure\n")
        raise SystemExit(3)


if __name__ == "__main__":
    main()
