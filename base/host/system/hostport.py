# meiva:note
# `Host`: the port to this machine's processes, executables and platform.
#
# A component that starts a process, looks up an executable or branches on the platform takes a
# `host: Host` as a required argument and calls `host.run`, `host.which`, `host.platform`,
# `host.uid` and `host.environ` instead of `subprocess`, `shutil.which`, `sys.platform`,
# `os.getuid` and `os.environ`. The composition root builds one with
# `Host.system(environ=...)`; a test builds the host it wants, with
# `FakeCommands` (`base/host/system/tests/fake_commands.py`) as `run` and plain values for the rest.
# A test that really needs a process uses `Host.system(environ={})` and patches nothing.
# Pure definitions take the planned command, paths and policy values instead of `Host`;
# the registration shell captures those inputs at its existing effect boundary.
#
# `run` is `base.host.proc.run_bounded`, not a second runner: its timeout bounds the whole process
# tree. The five fields are the whole port; another capability gets its own port or a reviewed
# addition here, so the object does not become a bag of everything the machine can do. `environ`
# is the process environment as a read-only snapshot, so a component asks the host about a display
# or a variable of the machine and a test builds a machine that has none; `run` does not use it, a
# caller that wants a child to run in it passes `env=` itself.

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from base.host.proc import CommandRunner, run_bounded
from base.native_process.os_platform import Platform
from base.native_process.root_control.client import current_uid

__all__ = ["Host"]


@dataclass(frozen=True)
class Host:
    """This machine as a component sees it: run a command, find a program, platform, user."""

    # `base.host.proc.run_bounded` in production.
    run: CommandRunner
    # `shutil.which` in production: a program name or path to its resolved path, or None.
    which: Callable[[str], str | None]
    platform: Platform
    # The real user id of the process (`os.getuid()`).
    uid: int
    # meiva:note
    # The environment variables of the process: the mapping the root hands in, held read-only.
    environ: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "environ", MappingProxyType(dict(self.environ)))

    @classmethod
    def system(cls, *, environ: Mapping[str, str]) -> Host:
        # meiva:note
        # The real machine; the composition root's constructor. The root names the environment it
        # holds (its configuration owner's `process_environment()`), so this constructor reads none.
        return cls(
            run=run_bounded,
            which=shutil.which,
            platform=Platform.current(),
            uid=current_uid(),
            environ=environ,
        )
