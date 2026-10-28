# meiva:note
# How the branch-protection audit says what it found: the text and the JSON it prints, the exit
# status a verdict earns, and which part of a failed `gh` call is worth showing. Values in.
from __future__ import annotations

import json
from collections.abc import Sequence

__all__ = ["error_detail", "outcome", "report_text"]


# meiva:note
# The verdict a run earns and the exit status it returns with: drift is 1, a clean contract 0.
def outcome(findings: Sequence[str]) -> tuple[str, int]:
    return ("drift", 1) if findings else ("ok", 0)


# meiva:note
# The report as the command prints it: JSON for scripts, one line per finding for a person, and
# the `AUDIT_RESULT=` line both forms end with.
def report_text(result: str, findings: Sequence[str], *, as_json: bool) -> str:
    if as_json:
        body = json.dumps({"result": result, "findings": list(findings)}, sort_keys=True)
    elif result == "ok":
        body = "Branch protection audit: no drift detected."
    else:
        body = "\n".join(
            [f"Branch protection audit: {result}."] + [f"- {finding}" for finding in findings]
        )
    return f"{body}\nAUDIT_RESULT={result}"


# meiva:note
# What to show about a failed call: the child's stderr when it wrote one, its message otherwise.
def error_detail(stderr: object, message: str) -> str:
    text = stderr.strip() if isinstance(stderr, str) else ""
    return text or message
