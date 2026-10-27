"""Child process for the `pool_ops` home tests: a from-scratch interpreter importing `pool_ops`.

Usage: `child_pool_ops.py <pool_dir|machine_name|import_side_effects>`; prints the result.
"""

import sys
from pathlib import Path

before = set(sys.modules)
from meiva_builtins.plugins.meiva_memory import pool_ops  # noqa: E402

after = set(sys.modules)


def resolve_meiva_home() -> Path:
    from base.paths.home import resolve_meiva_home as resolve

    return resolve()


fn = sys.argv[1]
if fn == "pool_dir":
    print(str(pool_ops.pool_dir(resolve_meiva_home())))
elif fn == "machine_name":
    print(pool_ops.machine_name(resolve_meiva_home()))
elif fn == "follows_variable":
    import os

    from base.agents.context import MeivaContext
    from meiva.sdk_surface import process_context
    from meiva_builtins.plugins.meiva_memory import sdk

    for home in sys.argv[2:]:
        os.environ["MEIVA_HOME"] = home
        process_context.unbind_context()
        process_context.bind_context(MeivaContext(meiva_home=Path(home)))
        print(pool_ops.pool_dir(resolve_meiva_home()), sdk.PATH)
elif fn == "import_side_effects":
    # Nothing beyond pool_ops itself and its own (stdlib) imports should load as a side effect --
    # in particular, no sibling submodule that does plugin/hook declaration (plugin.py, sdk.py,
    # services.py, setup.py, ...).
    print(
        ",".join(
            sorted(m for m in after - before if m.startswith("meiva_builtins.plugins.meiva_memory"))
        )
    )
else:
    raise ValueError(f"unknown fn {fn!r}")
