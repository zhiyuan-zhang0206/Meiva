from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scripts.provision.apt_lock import (
    EXIT_FREE,
    EXIT_HELD,
    holders,
    lock_is_free,
    main,
    wait_for_lock,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ACTIONS = (
    _REPO_ROOT / ".github/actions/install-playwright-deps/action.yml",
    _REPO_ROOT / ".github/actions/install-pg-redis/action.yml",
)

# meiva:note
# The holder is a real second process taking the same fcntl write lock apt takes, started
# from the repository rather than from a `-c` string so the restricted-Python rule can see
# it. A test that only created the lock file would pass against the bug.
# meiva:link scripts/provision/apt_lock.py
_HOLDER = Path(__file__).with_name("lock_holder.py")

# meiva:note
# The guard is only as good as the install steps using it, so the actions are checked too:
# every invocation that unpacks packages must have a wait before it in the same action.
# `--download-only` is excluded on purpose — it fetches archives and never touches the dpkg
# lock, so a wait there would be a rule with no failure behind it.
# meiva:link scripts/provision/docs/apt-lock.meiva.okf.md
_APT_INSTALL_MARKERS = (
    "playwright install-deps chromium",
    "apt-get install -y",
    "dpkg -i ",
)


class Holder:
    def __init__(self, lock: Path) -> None:
        # meiva:note
        # The child runs this repository's holder script, never a command string; the only
        # variable part is the lock path the test owns, so the directive below is about a
        # fixed interpreter and fixed script rather than untrusted input.
        # meiva:link scripts/provision/tests/lock_holder.py
        self._process = subprocess.Popen(  # noqa: S603
            [sys.executable, str(_HOLDER), str(lock)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self._process.stdout is not None
        assert self._process.stdout.readline().strip() == "locked"

    @property
    def pid(self) -> int:
        return self._process.pid

    def release(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


def test_a_free_lock_is_reported_without_waiting(tmp_path: Path) -> None:
    lock = tmp_path / "lock-frontend"
    lock.write_text("", encoding="utf-8")

    assert lock_is_free(lock) is True
    assert main(["--lock", str(lock), "--timeout", "0"]) == EXIT_FREE


def test_a_missing_lock_file_counts_as_free_and_is_not_created(tmp_path: Path) -> None:
    lock = tmp_path / "lock-frontend"

    assert lock_is_free(lock) is True
    assert lock.exists() is False


def test_a_held_lock_is_reported_when_the_ceiling_is_zero(tmp_path: Path) -> None:
    lock = tmp_path / "lock-frontend"
    lock.write_text("", encoding="utf-8")
    holder = Holder(lock)
    try:
        free, waited = wait_for_lock(lock, timeout=0.0, interval=0.05)
        assert free is False
        assert waited < 1.0
        assert main(["--lock", str(lock), "--timeout", "0", "--interval", "0.05"]) == EXIT_HELD
    finally:
        holder.release()


def test_the_wait_ends_when_the_holder_exits(tmp_path: Path) -> None:
    lock = tmp_path / "lock-frontend"
    lock.write_text("", encoding="utf-8")
    holder = Holder(lock)

    def release_soon() -> None:
        time.sleep(0.5)
        holder.release()

    releaser = threading.Thread(target=release_soon)
    releaser.start()
    try:
        free, waited = wait_for_lock(lock, timeout=10.0, interval=0.05)
        assert free is True
        assert 0.3 <= waited < 10.0
    finally:
        releaser.join(timeout=10)
        holder.release()


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="holder discovery reads /proc")
def test_the_holder_is_named_on_timeout(tmp_path: Path) -> None:
    lock = tmp_path / "lock-frontend"
    lock.write_text("", encoding="utf-8")
    holder = Holder(lock)
    try:
        assert holder.pid in holders(lock)
    finally:
        holder.release()


def _unguarded_installs(action: Path) -> tuple[list[str], bool]:
    lines = action.read_text(encoding="utf-8").splitlines()
    waits = [index for index, line in enumerate(lines) if "wait_apt_lock.sh" in line]
    installs = [
        (index, line.strip())
        for index, line in enumerate(lines)
        if any(marker in line for marker in _APT_INSTALL_MARKERS) and "--download-only" not in line
    ]
    return [line for index, line in installs if not any(wait < index for wait in waits)], bool(
        waits
    )


def test_every_install_step_waits_for_the_lock_first() -> None:
    offenders = {}
    for action in _ACTIONS:
        unguarded, has_wait = _unguarded_installs(action)
        if unguarded or not has_wait:
            offenders[action.parent.name] = unguarded or ["no wait at all"]
    assert offenders == {}
