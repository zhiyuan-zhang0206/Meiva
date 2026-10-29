# meiva:note
# The Google provider must keep its SDK import inside the function that needs it. `meiva/boot.py`
# builds the model catalog at every process start (`_load_inputs` -> `build_model_catalog`), and
# building it imports each enabled plugin's `provider.py`; the exec child boots the same way
# through `initialize_context`. A module-scope `google.genai` import here cost about 305ms of a
# 1.84s `execute_code` child, which never calls a model.
# Guard shape: the assertion reads this module's own AST rather than `sys.modules` or a timer.
# A sibling test that imports the SDK would make a `sys.modules` check pass vacuously, and a
# wall-clock threshold is the wrong instrument for "did this import happen at module scope".
# meiva:link base/lm/docs/provider-plugins.meiva.okf.md

from __future__ import annotations

import ast
from pathlib import Path

from meiva_builtins.plugins.lm_google import provider

_GOOGLE_SDK_ROOT = "google"


def _is_type_checking(test: ast.expr) -> bool:
    name = test.attr if isinstance(test, ast.Attribute) else getattr(test, "id", None)
    return name == "TYPE_CHECKING"


# meiva:note
# Module-scope imports that execute. A `TYPE_CHECKING` block never runs and a function body is the
# place the import belongs, so neither counts; the walk descends into every other statement
# (an `if`, `try` or `with` at module scope still imports when the module loads).
def _runtime_imports_at_module_scope(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    pending: list[ast.AST] = list(tree.body)
    found: list[ast.Import | ast.ImportFrom] = []
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.append(node)
            continue
        pending.extend(ast.iter_child_nodes(node))
    return found


def _names_google_sdk(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == _GOOGLE_SDK_ROOT
    return any(alias.name.split(".")[0] == _GOOGLE_SDK_ROOT for alias in node.names)


def test_the_google_sdk_is_not_imported_at_module_scope() -> None:
    source = Path(provider.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = [
        ast.get_source_segment(source, node)
        for node in _runtime_imports_at_module_scope(tree)
        if _names_google_sdk(node)
    ]

    assert offenders == [], (
        "the Google provider imports the google.genai SDK at module scope; every process that "
        f"builds the model catalog pays for it, including the exec child: {offenders}"
    )
