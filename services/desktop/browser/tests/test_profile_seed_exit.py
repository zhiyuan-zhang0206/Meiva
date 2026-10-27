import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

import psutil

from base.native_process.ownership import OwnedProcess


# meiva:note
# Both children hold real stdin pipes. EOF ends the lock-holder or an unfinished prompt;
# a wedged child is killed only through its captured native birth, then reaped finitely.
# The command is this interpreter and one literal repository entry, never a shell.
@contextmanager
def _child(
    mode: Literal["chrome", "seed"], home: Path, source: Path
) -> Generator[tuple[subprocess.Popen[bytes], OwnedProcess]]:
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_profile_seed_child.py")),
            mode,
            str(source),
            str(home / "chrome-profile"),
        ],
        env={**os.environ, "MEIVA_HOME": str(home), "MEIVA_CONFIG_FETCH": "skip"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    identity: OwnedProcess | None = None
    try:
        identity = OwnedProcess.capture(psutil.Process(process.pid))
        yield process, identity
    finally:
        assert process.stdin is not None
        with suppress(BrokenPipeError):
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            retained = (
                identity
                if identity is not None
                else OwnedProcess.capture(psutil.Process(process.pid))
            )
            retained.send_signal(signal.SIGKILL)
            process.wait(timeout=5)
        finally:
            assert process.stdout is not None
            process.stdout.close()


def _expect(process: subprocess.Popen[bytes], text: bytes, output: bytearray) -> None:
    assert process.stdout is not None
    deadline = time.monotonic() + 10
    while text not in output:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"profile prompt never arrived: {output!r}"
        assert select.select([process.stdout], [], [], remaining)[0], output
        chunk = os.read(process.stdout.fileno(), 4096)
        assert chunk, f"profile child exited before its prompt: {output!r}"
        output.extend(chunk)


def test_copy_proceeds_after_chrome_quits(tmp_path: Path) -> None:
    source = tmp_path / "daily-chrome"
    (source / "Default").mkdir(parents=True)
    (source / "Default" / "Cookies").write_bytes(b"actual-cookie-jar")
    home = tmp_path / "meiva-home"
    home.mkdir()
    destination = home / "chrome-profile"
    with _child("chrome", home, source) as (chrome, chrome_identity):
        (source / "SingletonLock").symlink_to(f"test-host-{chrome.pid}")
        with _child("seed", home, source) as (seed, seed_identity):
            output = bytearray()
            _expect(seed, b"(default new): ", output)
            assert seed.stdin is not None
            seed.stdin.write(b"copy\n")
            seed.stdin.flush()
            _expect(seed, b"Quit Chrome fully", output)
            assert chrome_identity.live()
            assert not destination.exists()
            assert chrome.stdin is not None
            chrome.stdin.close()
            assert chrome.wait(timeout=5) == 0
            assert not chrome_identity.live()
            seed.stdin.write(b"\nyes\n")
            seed.stdin.flush()
            assert seed.wait(timeout=10) == 0
            assert not seed_identity.live()
            assert (destination / "Default" / "Cookies").read_bytes() == b"actual-cookie-jar"
            assert not (destination / "SingletonLock").exists()
    assert not chrome_identity.live()
    assert not seed_identity.live()
