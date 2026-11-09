# meiva:note
# What the stored schedule scripts' offline check, run on the gateway under NEW's code, lets the
# window do: nothing to report, warn and go on, or refuse before anything stops. A red or an
# unevaluable check would crash-loop the schedule runner after `up`.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.md

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduleCheck:
    notes: tuple[str, ...]
    refusal: str | None


# meiva:note
# The check's output is its own `RED`/`TOOL-ERROR` lines and a closing `VERIFY_RC=`. `SKIPPED`
# means the host could not even be asked (a dry run does not fetch NEW), which is not a verdict.
def schedule_check(output: str, *, allow_red: bool, new: str) -> ScheduleCheck:
    del allow_red
    lines = output.splitlines()
    if any(line.startswith("SKIPPED") for line in lines):
        return ScheduleCheck(("stored schedule scripts: not checked (see above)",), None)
    code = next((line.partition("=")[2] for line in lines if line.startswith("VERIFY_RC=")), "?")
    if code == "0":
        return ScheduleCheck(tuple(line for line in lines if line.startswith("REUSED")), None)
    reds = [line for line in lines if line.startswith(("RED ", "TOOL-ERROR"))]
    what = "red" if code == "1" else f"unevaluable (check exited {code})"
    detail = "\n  ".join(reds) or "no RED line: see the output above"
    warning = f"WARNING: stored schedule scripts {what} under {new[:12]}, continuing:\n  {detail}"
    return ScheduleCheck((warning,), None)
