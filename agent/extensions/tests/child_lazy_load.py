# meiva:note
# One scenario of the lazy plugin load, run in a fresh interpreter by `test_lazy_plugin_load.py`:
# `python -I -B -X utf8 child_lazy_load.py <scenario> <load counter file>` imports `meiva` the way a
# persistent-shell script does, performs the scenario and prints one JSON report on its last line.
# The plugin under load appends a line to the counter file each time its surface module executes,
# so "loaded once" and "never loaded" are counts of real executions, not calls to a stand-in.
# meiva:link meiva/docs/boot-contracts.meiva.okf.md

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.paths import meiva_home


def sdk_namespace(module: object, name: str) -> Any:
    return getattr(module, name)


def _as_exec_entry() -> None:
    import importlib.machinery

    sys.modules["__main__"].__spec__ = importlib.machinery.ModuleSpec("agent.execution.child", None)


# meiva:note
# A bare Python process that holds a context carrying its Meiva home, the one thing the SDK
# reads it from; no identity, no clients.
def _bind_home() -> None:
    from base.agents.context import MeivaContext
    from base.paths.home import resolve_meiva_home
    from meiva.sdk_surface import process_context

    process_context.bind_context(MeivaContext(meiva_home=resolve_meiva_home(), environ=os.environ))


def _executions(counter: Path) -> int:
    return len(counter.read_text(encoding="utf-8").splitlines()) if counter.exists() else 0


def _installed() -> bool:
    from meiva.sdk_surface.installation import installed

    return installed() is not None


def _attribute_error(read: Callable[[], object]) -> str | None:
    try:
        read()
    except AttributeError as error:
        return str(error)
    return None


def _repeated_failure(attempts: list[Callable[[], object]]) -> dict[str, Any]:
    errors: list[BaseException] = []
    for attempt in attempts:
        try:
            attempt()
        except Exception as error:
            errors.append(error)
    return {
        "raised": len(errors),
        "same_error": all(error is errors[0] for error in errors),
        "type": type(errors[0]).__name__ if errors else None,
    }


def loads_at_import(counter: Path) -> dict[str, Any]:
    import meiva

    return {
        "installed_after_import": _installed(),
        "pong": sdk_namespace(meiva, "lazytasks").ping(),
        "executions": _executions(counter),
    }


def loads_on_first_miss(counter: Path) -> dict[str, Any]:
    _as_exec_entry()
    import meiva

    after_import = _installed()
    return {
        "installed_after_import": after_import,
        "pong": sdk_namespace(meiva, "lazytasks").ping(),
        "installed": _installed(),
        "executions": _executions(counter),
    }


def latched_after_first_load(counter: Path) -> dict[str, Any]:
    _as_exec_entry()
    import meiva

    _ = sdk_namespace(meiva, "lazytasks")
    second = _attribute_error(lambda: sdk_namespace(meiva, "still_unknown"))
    return {"second_miss": second, "executions": _executions(counter)}


def unknown_name(counter: Path) -> dict[str, Any]:
    import meiva

    return {
        "error": _attribute_error(lambda: sdk_namespace(meiva, "nope_xyz")),
        "installed": _installed(),
        "executions": _executions(counter),
    }


def private_name(counter: Path) -> dict[str, Any]:
    _as_exec_entry()
    import meiva

    private = "_some_private"
    return {
        "error": _attribute_error(lambda: getattr(meiva, private)),
        "installed": _installed(),
        "executions": _executions(counter),
    }


def agent_process(counter: Path) -> dict[str, Any]:
    import meiva
    from base.agents.context import MeivaContext
    from base.agents.context.identity import AgentIdentity
    from meiva.sdk_surface import process_context

    process_context.bind_context(
        MeivaContext(identity=AgentIdentity(agent_id=7, owns_loop=True), environ=os.environ)
    )
    return {
        "error": _attribute_error(lambda: sdk_namespace(meiva, "nope_xyz")),
        "executions": _executions(counter),
    }


def forwarded_setting(counter: Path) -> dict[str, Any]:
    _as_exec_entry()
    import meiva

    return {"db_url_is_str": isinstance(meiva.DB_URL, str), "executions": _executions(counter)}


def ensure_twice(counter: Path) -> dict[str, Any]:
    import meiva

    _bind_home()
    meiva.ensure_plugins_loaded()
    meiva.ensure_plugins_loaded()
    return {"installed": _installed(), "executions": _executions(counter)}


def unreadable_plugin_config(counter: Path) -> dict[str, Any]:
    import meiva
    from meiva.sdk_surface import install

    _bind_home()
    report = _repeated_failure([meiva.ensure_plugins_loaded, meiva.ensure_plugins_loaded])
    return {
        **report,
        "installed": _installed(),
        "attempted": install.load_attempted(),
        "executions": _executions(counter),
    }


def unreadable_config_on_lookup(counter: Path) -> dict[str, Any]:
    _as_exec_entry()
    import meiva

    return {
        **_repeated_failure(
            [
                lambda: sdk_namespace(meiva, "missing_after_failure"),
                lambda: sdk_namespace(meiva, "missing_after_failure"),
            ]
        ),
        "executions": _executions(counter),
    }


def faces_failure(counter: Path) -> dict[str, Any]:
    import meiva
    from base.packages.plugins.enable_config import local_config_path
    from meiva.sdk_surface.installation import installed

    _bind_home()
    meiva.ensure_plugins_loaded()
    surface = installed()
    assert surface is not None and not surface.faces
    local_config_path(home=meiva_home()).write_text("{not json", encoding="utf-8")
    report = _repeated_failure(
        [
            lambda: meiva.ensure_plugins_loaded(surface=False),
            lambda: meiva.ensure_plugins_loaded(surface=True),
            lambda: meiva.ensure_plugins_loaded(surface=False),
        ]
    )
    return {
        "faces": report,
        "still_the_surface": installed() is surface and not surface.faces,
        "executions": _executions(counter),
    }


def member_on_framework_namespace(counter: Path) -> dict[str, Any]:
    _as_exec_entry()
    import meiva

    member = "lazylog"
    return {"pong": getattr(meiva.self, member)(), "executions": _executions(counter)}


def missing_member(counter: Path) -> dict[str, Any]:
    import meiva

    self_member, ui_member = "lazylog", "lazynotify"
    return {
        "self": _attribute_error(lambda: getattr(meiva.self, self_member)),
        "ui": _attribute_error(lambda: getattr(meiva.ui, ui_member)),
        "executions": _executions(counter),
    }


SCENARIOS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "loads_at_import": loads_at_import,
    "loads_on_first_miss": loads_on_first_miss,
    "latched_after_first_load": latched_after_first_load,
    "unknown_name": unknown_name,
    "private_name": private_name,
    "agent_process": agent_process,
    "forwarded_setting": forwarded_setting,
    "ensure_twice": ensure_twice,
    "unreadable_plugin_config": unreadable_plugin_config,
    "unreadable_config_on_lookup": unreadable_config_on_lookup,
    "faces_failure": faces_failure,
    "member_on_framework_namespace": member_on_framework_namespace,
    "missing_member": missing_member,
}


if __name__ == "__main__":
    sys.stdout.write(json.dumps(SCENARIOS[sys.argv[1]](Path(sys.argv[2]))) + "\n")
