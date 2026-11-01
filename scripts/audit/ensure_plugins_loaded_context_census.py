# meiva:note
# Census of every `ensure_plugins_loaded(...)` call site and how its context is established.
#
# #8062 made the host-version gate take the environment its `git` runs under from the caller
# (`MeivaContext.require_environ()`, no fallback). A load site that runs under no context — or
# under one built without `environ=` — then raises on the first name listing (the skill scan,
# `meiva.agents.commands()`, the metering walk), and one such failure latches the whole process
# through `install.mark_load_failed()`. That family of misses kept arriving one call site at a
# time, so this census counts them instead of trusting "my batch is green".
#
# Three states per call site:
#   EXPLICIT  the call passes `context=...` itself: an inline `MeivaContext(...)` that names
#             `environ=`, or a context handed in by the caller (its provenance is decided there).
#   BOUND     no `context=`, and the file runs after something bound one. Every such file is
#             listed in `BOUND_REASONS` with the reason, so adding one is a reviewed decision
#             and the list cannot quietly become an allowlist.
#   UNBOUND   no usable context: no `context=` and no binding to inherit, or an inline
#             `MeivaContext(...)` that names no `environ=`. This count must be 0.
#
# Baselines (main `5a63c8014`, 2026-11-01): the parser sees 30 call expressions; the runtime
# denominator is 31, because `agent/tests/execution/test_context_binding.py` keeps one child
# script in a string and that script calls `meiva.ensure_plugins_loaded(surface=False)` when it
# runs. A text search sees 38 lines on the same tree — 7 of them are comments or assertions on
# generated script text — so a grep count is never the number of call sites, which is why this
# census parses with `ast`.
#
# Usage: python scripts/audit/ensure_plugins_loaded_context_census.py [repo root]
# Exit code: 1 while any call site is UNBOUND.

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CallSite", "census", "main"]

# meiva:note
# Directories that hold no first-party source: a virtualenv, dependencies, caches, VCS data,
# other checkouts and build output.
SKIP = frozenset({".venv", "node_modules", "__pycache__", ".git", ".worktrees", "dist", "build"})

# meiva:note
# Why a file that calls `ensure_plugins_loaded()` without naming a context still runs under one.
# A production entry binds before the call; the SDK's launcher paths prepare their context first;
# a bare child that binds nothing is deliberate, because with no bound context the SDK answers an
# empty name list instead of inventing a home, and those children need plugin namespaces only.
BOUND_REASONS = {
    "agent/execution/child.py": (
        "the exec child binds MeivaContext(environ=os.environ) at entry, before the state slot "
        "materializes and loads the surface"
    ),
    "agent/extensions/tests/child_lazy_load.py": (
        "the probe binds a context carrying its Meiva home and the environment it launched with "
        "before the first load"
    ),
    "agent/tests/execution/child_lazy_imports.py": (
        "a no-context child: the SDK lists no skill names, and it needs the plugin namespaces only"
    ),
    "meiva/__init__.py": (
        "launched scripts only: boot prepares a launched context carrying the launch mapping "
        "before the automatic load"
    ),
    "meiva/boot.py": (
        "the missing-name load for a launched child runs after boot prepared that context"
    ),
    "meiva/tests/bootstrap/child_sdk_disable.py": (
        "a no-context child: it reads the disable matrix, not the name index"
    ),
    "meiva/tests/bootstrap/child_sdk_probes.py": (
        "a no-context child: it probes the process posture, not the name index"
    ),
    "meiva/tests/namespaces/sdk_surface/base_config/_service_read_lite_child.py": (
        "a no-context child: it probes the config authority, not the name index"
    ),
    "meiva/tests/sdk_events/sdk_exit_child.py": (
        "an attachment child: the attachment binds its own context before the load"
    ),
    "meiva/tests/sdk_events/test_sdk_event_producer.py": (
        "an attachment binds its own context before the load"
    ),
    "schedules/log-cred-mask-monthly-schedule.py": (
        "runs inside the schedule runner, which binds the schedule actor context (with the "
        "config owner's environment) before the script"
    ),
    "schedules/sched-dry-import-weekly-schedule.py": (
        "runs inside the schedule runner, which binds the schedule actor context (with the "
        "config owner's environment) before the script"
    ),
    "tests/integration/child_schedule_runner.py": (
        "the preload runs before the caller binds; `engine.script_context` names the context the "
        "engine then loads under"
    ),
}


# meiva:note
# One call expression, the file it lives in, and how its context is established.
@dataclass(frozen=True, slots=True)
class CallSite:
    path: str
    line: int
    state: str

    @property
    def unbound(self) -> bool:
        return self.state == "UNBOUND"


def _call_state(call: ast.Call, path: str) -> str:
    for keyword in call.keywords:
        if keyword.arg != "context":
            continue
        value = keyword.value
        if isinstance(value, ast.Call):
            func = value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "MeivaContext" and not any(kw.arg == "environ" for kw in value.keywords):
                return "UNBOUND"
        return "EXPLICIT"
    return "BOUND" if path in BOUND_REASONS else "UNBOUND"


def _is_load_call(call: ast.Call) -> bool:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name == "ensure_plugins_loaded"


def _file_sites(path: Path, relative: str) -> list[CallSite]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    sites: list[CallSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_load_call(node):
            sites.append(CallSite(relative, node.lineno, _call_state(node, relative)))
    return sites


# meiva:note
# Every load call site under `root`, in file then line order.
def census(root: Path) -> list[CallSite]:
    sites: list[CallSite] = []
    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root)
        if any(part in SKIP for part in relative_path.parts):
            continue
        sites.extend(_file_sites(path, relative_path.as_posix()))
    return sites


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    root = Path(arguments[0] if arguments else ".").resolve()
    sites = census(root)
    for state in ("EXPLICIT", "BOUND", "UNBOUND"):
        group = [site for site in sites if site.state == state]
        print(f"{state}: {len(group)}")
        for site in group:
            print(f"   {site.path}:{site.line}")
    unbound = sum(1 for site in sites if site.unbound)
    print(f"TOTAL {len(sites)} sites; unbound={unbound}")
    return 1 if unbound else 0


if __name__ == "__main__":
    raise SystemExit(main())
