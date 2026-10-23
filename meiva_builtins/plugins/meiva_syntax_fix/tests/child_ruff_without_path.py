import json
import shutil

from meiva_builtins.plugins.meiva_syntax_fix._imports import detect_missing_imports
from meiva_builtins.plugins.meiva_syntax_fix._ruff import ruff_format
from meiva_builtins.plugins.meiva_syntax_fix._ruff_run import system_ruff


def _assert_ruff_unreachable() -> None:
    # meiva:note
    # host-ok: this child asks its own PATH, which the parent test emptied, whether ruff is
    # reachable.
    assert shutil.which("ruff") is None, "precondition: PATH cannot find ruff"


_assert_ruff_unreachable()
ruff = system_ruff()
print(
    json.dumps([ruff_format("x=1\n", ruff), detect_missing_imports("json.dumps({'a': 1})", ruff)])
)
