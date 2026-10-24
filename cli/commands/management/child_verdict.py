# meiva:note
# What the dry-import child's last stdout line means as a red detail. The child
# (`schedule_verify_child`) prints one verdict line; this module turns it, or the way it failed to
# run, into the text `RED id=... missing=...` carries: `<module>` for a missing import,
# `compile-error:<l>:<m>`, `<Exc>:<msg>`, `undefined-name:<l> <name>`,
# `call-signature:<l> <call>: <why>` and `tool-error:...` for a child that gave no verdict.
# meiva:link cli/docs/operator-surfaces/operator-surfaces.meiva.okf.md

from __future__ import annotations

# meiva:note
# The child's last stdout line, as (marker, prefix added to the line's remainder).
_VERDICTS = (
    ("CHILD-COMPILE-ERROR:", "compile-error:"),
    ("CHILD-MODULE:", ""),
    ("CHILD-EXC:", ""),
    ("CHILD-UNDEF:", "undefined-name:"),
    ("CHILD-SIG:", "call-signature:"),
)


# meiva:note
# None is clean (`CHILD-OK`). A child that printed no known verdict is a tool error carrying the
# last line of its stderr, or its exit code when it said nothing.
def child_verdict(stdout: str, stderr: str, returncode: int) -> str | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if last == "CHILD-OK":
        return None
    for marker, prefix in _VERDICTS:
        if last.startswith(marker):
            return prefix + last[len(marker) :]
    tail = (stderr.strip().splitlines() or [""])[-1][:140]
    return f"tool-error:{tail or f'rc={returncode}'}"


# meiva:note
# A child still running at its budget is wedged and must not park the sweep behind it.
def timeout_verdict(budget_s: float) -> str:
    return f"timeout>{budget_s:.0f}s"


def spawn_failure_verdict(error_type: str, message: str) -> str:
    return f"tool-error:{error_type}:{message[:100]}"
