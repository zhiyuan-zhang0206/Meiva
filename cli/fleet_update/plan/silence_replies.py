# meiva:note
# The alert silence that surrounds a window, as the shipped program is asked and as it answers.
# A silence that cannot open or close costs the operator a noisier window, never the window
# itself: the program prints `SILENCE failed` or `SILENCE skipped` and the window warns and goes
# on.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import shlex

from .host_commands import PYTHON


# meiva:note
# The command that runs the shipped silence program on the gateway host, which reads the program
# from stdin. Opening carries the window's length and the comment the silence is listed under.
def silence_command(verb: str, *, hours: float | None, comment: str) -> str:
    if verb == "open":
        if hours is None:
            raise ValueError("opening a silence needs its length in hours")
        return f"{PYTHON} - open --hours {hours:g} --comment {shlex.quote(comment)}"
    if verb == "close":
        return f"{PYTHON} - close"
    raise ValueError(f"unknown silence verb {verb!r}")


def silence_warnings(verb: str, output: str) -> tuple[str, ...]:
    return tuple(
        f"WARNING: alert silence not {verb}: {line.removeprefix('SILENCE ')}"
        for line in output.splitlines()
        if line.startswith(("SILENCE failed", "SILENCE skipped"))
    )
