# meiva:note
# Whether a root the window keeps could serve NEW, asked of each host before anything stops. A kept
# root (`--keep-shell-sessions` or a kept service) is reused by the `up` half's start only when
# NEW would request the root's own launch generation; otherwise that start is refused after every
# service has stopped and the cluster stays down. The answer is NEW's own
# (`cli/commands/lifecycle/live_generation.py`, run from a throwaway worktree of NEW), so what
# differs is decided by the code that will refuse, not guessed from the diff.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.md

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from .host_commands import HOME

# meiva:note
# The program run inside NEW's worktree: its start entry, taking the host's checkout path, the
# service selection and its names as arguments, and the host's home from its own settings.
ROOT_CHECK_PROGRAM = (
    "import sys; from base.paths import meiva_home; "
    "from cli.commands.lifecycle.live_generation import main; "
    "sys.exit(main(sys.argv[1:], home=meiva_home()))"
)

_NO_ROOT = "none"
_SAME = "same"
_CHANGED = "changed: "


# meiva:note
# Whether the stop choices leave the application root running: it is the pty owner's host unless
# every unit stops, so keeping the shell sessions or any service keeps it.
def root_is_kept(*, shell: str, kept: str) -> bool:
    return shell == "--keep-shell-sessions" or kept != "--keep-no-services"


def root_check_script(template: str, *, new: str, selection: str, names: Sequence[str]) -> str:
    return template.format(
        home=HOME,
        new=new,
        program=shlex.quote(ROOT_CHECK_PROGRAM),
        selection=" ".join(shlex.quote(word) for word in (selection, *names)),
    )


@dataclass(frozen=True)
class RootCheck:
    notes: tuple[str, ...]
    refusal: str | None


# meiva:note
# One host's answer: a root of another generation refuses the window (exit 2, nothing touched); a
# check that could not run warns and goes on, as the stored-schedule check does, because it is a
# prediction of the start that will say the same thing for real.
def root_check(alias: str, output: str, *, new: str) -> RootCheck:
    lines = output.splitlines()
    if any(line.startswith("SKIPPED") for line in lines):
        return RootCheck((f"{alias}: kept-root generation: not checked (see above)",), None)
    code = next(
        (line.partition("=")[2] for line in lines if line.startswith("ROOT_CHECK_RC=")), "?"
    )
    verdict = next(
        (line.partition("=")[2] for line in lines if line.startswith("ROOT_GENERATION=")), None
    )
    if code == "0" and verdict in (_NO_ROOT, _SAME):
        return RootCheck((), None)
    if code == "0" and verdict is not None and verdict.startswith(_CHANGED):
        detail = verdict.removeprefix(_CHANGED)
        return RootCheck(
            (),
            f"{alias}: the meiva-root this window keeps cannot serve {new[:12]}: {detail}. "
            "The `up` half would be refused after every service stopped. Choose "
            "--close-shell-sessions with --keep-no-services, so the root restarts on the new code",
        )
    return RootCheck(
        (
            f"WARNING: {alias}: could not tell whether its kept meiva-root can serve {new[:12]} "
            f"(check exited {code}), continuing: see the output above",
        ),
        None,
    )
