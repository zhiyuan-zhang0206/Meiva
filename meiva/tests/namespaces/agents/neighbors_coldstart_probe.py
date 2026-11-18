# meiva:note
# Child process for the cold-start `get_neighbors` / `shell.sessions.capture` regression tests:
# each scenario needs a fresh interpreter with only stdlib imported before `import meiva`.
# Usage: `neighbors_coldstart_probe.py <repo root> <mode> <target_id>`

from __future__ import annotations

import json
import sys


def main() -> None:
    root, mode, target_id = sys.argv[1], sys.argv[2], int(sys.argv[3])
    sys.path.insert(0, root)
    import meiva
    from meiva.sdk_surface.installation import installed

    installed_at_import = installed() is not None
    report: dict[str, object] = {"installed_at_import": installed_at_import}

    if mode == "neighbors":
        kwargs: dict[str, int] = {}
        if len(sys.argv) > 4 and sys.argv[4] != "-":
            kwargs["depth"] = int(sys.argv[4])
        if len(sys.argv) > 5 and sys.argv[5] != "-":
            kwargs["limit"] = int(sys.argv[5])
        neighbors = meiva.agents.get_neighbors(target_id, **kwargs)
        report["installed_after"] = installed() is not None
        report["neighbors"] = [
            {
                "agent_id": n.agent_id,
                "label": n.label,
                "status": n.status.value,
                "depth": n.depth,
                "score": n.score,
            }
            for n in neighbors
        ]
    elif mode == "neighbors_error":
        try:
            _ = meiva.agents.get_neighbors(target_id)
        except BaseException as exc:
            report["error"] = type(exc).__name__
            report["message"] = str(exc)
        else:
            report["error"] = None
    elif mode == "capture_error":
        try:
            _ = meiva.shell.sessions.capture(target_id)
        except BaseException as exc:
            report["error"] = type(exc).__name__
            report["message"] = str(exc)
        else:
            report["error"] = None
        report["installed_after"] = installed() is not None
    elif mode == "config_authority":
        from meiva.sdk_surface.settings import config_authority

        report["depth"] = config_authority().service_field_value("neighbors_default_depth")
        report["installed_after"] = installed() is not None
    else:
        raise ValueError(f"unknown probe mode: {mode!r}")

    print("REPORT " + json.dumps(report))


if __name__ == "__main__":
    main()
