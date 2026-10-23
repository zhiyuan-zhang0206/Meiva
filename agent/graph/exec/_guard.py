# meiva:note
# What the agent is told when the editable-install guard stops an exec before any child starts:
# a poisoned install that names its records, or an inspection that itself failed.
# meiva:link agent/graph/exec/docs/exec.meiva.okf.md

from __future__ import annotations

from ._result import ExecChildError, ExecCrashed


def inspection_refusal(error: Exception) -> ExecCrashed:
    return ExecCrashed(
        output=(
            f"exec editable-install guard could not inspect the interpreter: {error}\n\n"
            "Do not retry this execute_code call. Report the error to the operator."
        ),
        exc=error,
    )


def violation_refusal(violations: tuple[str, ...]) -> ExecCrashed | None:
    if not violations:
        return None
    output = (
        "exec editable install is invalid and no child was started. "
        "Do not retry this execute_code call. Ask the operator to run meiva converge "
        "on this host and inspect the source records before starting new work.\n\n"
        "Invalid records:\n" + "\n".join(f"- {violation}" for violation in violations)
    )
    return ExecCrashed(
        output=output,
        exc=ExecChildError("exec_editable_install_poisoned", output, None),
    )
