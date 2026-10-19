from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

from scripts.structure import imports, placement
from scripts.structure.tests.patch_repo import make_repo, write

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _pytest_child_env() -> dict[str, str]:
    # meiva:note
    # The environment for a pytest child: this process's, with plugin autoload off.
    # The variable is handed to the child explicitly instead of being written into
    # this process's environment for the duration of the test.
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


@pytest.mark.parametrize("package_tests", [False, True])
def test_pytest_importlib_relative_imports_match_ast_with_or_without_test_initializer(
    tmp_path: pathlib.Path, package_tests: bool
) -> None:
    root = make_repo(tmp_path)
    initializer = "from .retry import backoff as local_backoff\n"
    write(root, "base/net/__init__.py", initializer)
    if package_tests:
        write(root, "base/net/tests/__init__.py", "")
    rel = "base/net/tests/test_relative.py"
    text = (
        "from .. import retry as subject\n"
        "from ..retry import backoff as call\n"
        "from .. import local_backoff as exported\n\n"
        "import base.net.retry as absolute_subject\n"
        "from ..retry import backoff as first, backoff as second\n\n"
        "def test_relative_contract():\n"
        "    from ..retry import backoff as lazy\n"
        "    assert __package__ == 'base.net.tests'\n"
        "    assert call() == subject.backoff() == exported() == absolute_subject.backoff() == 1\n"
        "    assert first() == second() == lazy() == 1\n"
    )
    write(root, rel, text)
    # meiva:note
    # Inner pytest run: this checkout's interpreter against a synthetic test tree,
    # with plugin autoload off in the child's own environment.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--import-mode=importlib",
            "--confcutdir",
            str(root),
            "-o",
            "addopts=",
            rel,
        ],
        cwd=root,
        env=_pytest_child_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    index = placement.ModuleIndex(root)
    refs = placement.collect_references(ast.parse(text), index, rel)
    assert [ref.module for ref in refs] == [
        "base.net.retry",
        "base.net.retry",
        "base.net",
        "base.net.retry",
        "base.net.retry",
        "base.net.retry",
    ]
    assert refs[4].names == ("first", "second")
    init_refs = placement.collect_references(ast.parse(initializer), index, "base/net/__init__.py")
    assert [(ref.module, ref.names) for ref in init_refs] == [
        ("base.net.retry", ("local_backoff",))
    ]


@pytest.mark.parametrize(
    ("rel", "text", "module", "error"),
    [
        ("base/__init__.py", "from ..base.net import retry", "base", "beyond top-level package"),
        ("standalone.py", "from .base.net import retry", "standalone", "no known parent package"),
    ],
)
def test_relative_imports_cannot_escape_the_package_into_repository_modules(
    pytester: pytest.Pytester, rel: str, text: str, module: str, error: str
) -> None:
    root = make_repo(pytester.path)
    write(root, rel, text)
    probe = pytester.makepyfile(probe=f"import importlib\nimportlib.import_module({module!r})\n")
    result = pytester.runpython(probe)
    assert result.ret != 0
    assert error in "\n".join(result.errlines)
    with pytest.raises(imports.InvalidRelativeImportError, match=rel):
        placement.collect_references(ast.parse(text), placement.ModuleIndex(root), rel)
