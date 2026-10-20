import os
from pathlib import Path

import pytest

from base.native_process.root_control.instance_lock import (
    AlreadyRunningError,
    acquire_instance_lock,
    release_instance_lock,
)


def test_a_second_holder_of_the_same_lock_is_refused(tmp_path: Path) -> None:
    first = acquire_instance_lock(tmp_path, "agent_host.lock")
    try:
        with pytest.raises(AlreadyRunningError, match=r"agent_host\.lock"):
            acquire_instance_lock(tmp_path, "agent_host.lock")
    finally:
        release_instance_lock(first)
    release_instance_lock(acquire_instance_lock(tmp_path, "agent_host.lock"))


def test_locks_with_different_names_do_not_conflict(tmp_path: Path) -> None:
    host = acquire_instance_lock(tmp_path, "agent_host.lock")
    root = acquire_instance_lock(tmp_path)
    try:
        assert (tmp_path / "meiva-root.lock").read_text() == f"{os.getpid()}\n"
    finally:
        release_instance_lock(host)
        release_instance_lock(root)


def test_the_lock_fd_is_not_inherited_by_children(tmp_path: Path) -> None:
    fd = acquire_instance_lock(tmp_path, "agent_host.lock")
    try:
        assert not os.get_inheritable(fd)
    finally:
        release_instance_lock(fd)
