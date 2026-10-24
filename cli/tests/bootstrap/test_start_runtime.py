from __future__ import annotations

import sys
from pathlib import Path

import pytest

from base.host import runtime_interpreter
from base.host.runtime_interpreter import LoadedRuntimeError
from cli.start_runtime import StartRuntime


def test_loaded_runtime_package_is_the_checkout_it_was_imported_from() -> None:
    assert runtime_interpreter.loaded_runtime()[2] == Path(__file__).resolve().parents[3]


# meiva:note
# The one place the identity is verified: a start holds the value, it does not re-read it.
def test_admit_holds_the_identity_of_the_checkout_and_interpreter_that_loaded_the_code() -> None:
    checkout = runtime_interpreter.loaded_runtime()[2]

    runtime = StartRuntime.admit(checkout)

    assert runtime.code_root == runtime.cwd == checkout
    assert runtime.interpreter == Path(sys.executable).absolute()
    assert runtime.loaded.kind == "source"
    assert runtime.loaded.code_root == runtime.loaded.cwd == str(checkout)
    assert runtime.loaded.interpreter == str(Path(sys.executable).resolve())
    assert runtime.loaded.source_digest is not None
    assert runtime.module_argv("services.supervision.meiva_root", "--run-dir", "/run") == [
        str(runtime.interpreter),
        "-m",
        "services.supervision.meiva_root",
        "--run-dir",
        "/run",
    ]


def test_admit_refuses_a_checkout_that_did_not_load_the_code(tmp_path: Path) -> None:
    with pytest.raises(LoadedRuntimeError, match="canonical checkout"):
        StartRuntime.admit(tmp_path)
