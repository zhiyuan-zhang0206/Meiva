# meiva:note
# Child process for the harness-attach tests: each scenario needs a fresh interpreter.
# Usage: `harness_attach_probes.py <repo root> <marker file>`. The root goes on
# `sys.path` and the marker is a file the probe reads, so the SDK is imported exactly
# as a provider's tool call imports it — no pytest fixture has run in this process,
# and the environment is whatever the test passed in.
# meiva:link meiva/self/harness/docs/harness-attach.md

import json
import sys
from typing import Any


def main() -> None:
    root, marker_path = sys.argv[1], sys.argv[2]
    sys.path.insert(0, root)
    import meiva

    report: dict[str, Any] = {"has_tasks_at_import": "tasks" in dir(meiva)}

    def attempt(label: str, call: Any) -> None:
        try:
            report[label] = {"ok": repr(call())[:120]}
        except BaseException as exc:
            report[label] = {"error": type(exc).__name__, "message": str(exc)}

    if len(sys.argv) > 3:
        report["ui_source"] = meiva.ui.__file__
        operation = sys.argv[3]
        calls = {
            "ask": lambda: meiva.ui.ask([{"title": "Isolated invalid-session probe"}]),
            "publish": lambda: meiva.ui.publish("Isolated invalid-session probe"),
            "close": lambda: meiva.ui.close_question(1),
        }
        attempt("ui_first", calls[operation])
        print("REPORT " + json.dumps(report))
        return

    attempt("files_read", lambda: meiva.files.read(marker_path).strip())
    attempt("shell_run", lambda: meiva.shell.run("echo hi").strip())
    attempt("agent_id", lambda: meiva.self.AGENT_ID)
    attempt("agent_id_again", lambda: meiva.self.AGENT_ID)
    attempt("context", lambda: meiva.context)
    attempt("tasks", lambda: meiva.tasks)
    attempt("agents_send", lambda: meiva.agents.send_message(1, "x"))
    print("REPORT " + json.dumps(report))


if __name__ == "__main__":
    main()
