# meiva:note
# Child process for the cold-start `meiva.self` regression tests: each scenario runs in a fresh
# interpreter with only stdlib imported before `import meiva`, verifying `bound_context()` is None
# before the first `meiva.self.*` or `agent_identity` / `settings` call.
# Usage: `self_coldstart_probe.py <repo root> <mode>`

from __future__ import annotations

import json
import sys


def _invoke(mode: str) -> object:
    import meiva
    from meiva.sdk_surface import agent_identity, settings

    if mode == "restart":
        return meiva.self.restart("cold restart")
    if mode == "restart_switch":
        return meiva.self.restart(
            "cold switch",
            config_overlay={"harness": "antigravity", "llm_model": "gemini-4-neon"},
        )
    if mode == "terminate":
        meiva.self.terminate()
    if mode == "pause_heartbeat":
        meiva.self.pause_heartbeat(90)
        return "ok"
    if mode == "set_label":
        meiva.self.set_label("cold-label")
        return "ok"
    if mode == "compact":
        meiva.self.compact("cold summary")
    if mode == "attach":
        meiva.self.attach("cold-probe.png")
        return "ok"
    if mode == "process_harness_name":
        return agent_identity.process_harness_name()
    if mode == "assert_self_action":
        agent_identity.assert_self_action("restart")
        return "ok"
    if mode == "config_authority":
        _ = settings.config_authority()
        return "ok"
    if mode == "clock":
        _ = settings.clock()
        return "ok"
    if mode == "rng":
        _ = settings.rng()
        return "ok"
    raise ValueError(f"unknown probe mode: {mode!r}")


def main() -> None:
    root, mode = sys.argv[1], sys.argv[2]
    sys.path.insert(0, root)
    import meiva
    from meiva.sdk_surface import process_context
    from meiva.sdk_surface.installation import installed

    _ = meiva
    report: dict[str, object] = {
        "bound_before": process_context.bound_context() is not None,
        "installed_at_import": installed() is not None,
    }

    try:
        report["result"] = _invoke(mode)
    except SystemExit as exc:
        report["exit_code"] = exc.code
    except BaseException as exc:
        report["error"] = type(exc).__name__
        report["message"] = str(exc)

    report["bound_after"] = process_context.bound_context() is not None
    report["installed_after"] = installed() is not None
    print("REPORT " + json.dumps(report))


if __name__ == "__main__":
    main()
