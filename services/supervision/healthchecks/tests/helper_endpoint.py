# meiva:note
# A real helper endpoint on a real unix socket, and real processes to stand in as the root.

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psutil

from base.native_process.ownership import OwnedProcess

__all__ = ["live_root", "serving", "socket_dir", "under_other_parent", "wire_reply"]


# meiva:note
# A directory whose socket paths fit AF_UNIX (about 104 bytes; pytest's tmp_path is longer).
@contextmanager
def socket_dir() -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="meiva-helper-", dir="/tmp") as directory:
        yield Path(directory)


# meiva:note
# The helper's reply to `ping`, naming `pid` as the process that answered (None leaves it out).
def wire_reply(pid: int | None, *, pong: bool = True) -> bytes:
    result: dict[str, object] = {"pong": pong, "preflight_screen": True, "ax_trusted": True}
    if pid is not None:
        result["pid"] = pid
    return (json.dumps({"id": 0, "ok": True, "result": result}) + "\n").encode()


# meiva:note
# A listener on `path` that answers the first request of its first connection with `reply`; a
# client that hangs up without asking is answered with nothing. It yields the requests received, so
# a test reads the bytes the client really sent.
@contextmanager
def serving(path: Path, reply: bytes) -> Generator[list[bytes]]:
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            request = conn.recv(4096)
            received.append(request)
            if request:
                conn.sendall(reply)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield received
    finally:
        server.close()
        worker.join(timeout=2)


# meiva:note
# A real process whose native parent is this test process: the root, as the helper's ancestry
# check sees it when the helper is the process serving the socket.
@contextmanager
def live_root() -> Generator[OwnedProcess]:
    child = subprocess.Popen(["/bin/sleep", "60"])
    try:
        yield OwnedProcess.capture(psutil.Process(child.pid))
    finally:
        child.kill()
        child.wait()


# meiva:note
# A real process whose native parent is a shell, not this test process: a root the helper this
# test serves is not the parent of.
@contextmanager
def under_other_parent() -> Generator[OwnedProcess]:
    shell = subprocess.Popen(
        ["/bin/sh", "-c", "/bin/sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, text=True
    )
    assert shell.stdout is not None
    grandchild = psutil.Process(int(shell.stdout.readline()))
    try:
        yield OwnedProcess.capture(grandchild)
    finally:
        grandchild.kill()
        shell.kill()
        shell.wait()
        shell.stdout.close()
