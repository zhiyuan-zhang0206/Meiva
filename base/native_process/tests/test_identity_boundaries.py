from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from base.native_process.birth_key import process_birth_key


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("ticks", [1, 123])
def test_positive_ticks_are_the_birth_identity(platform: str, ticks: int) -> None:
    assert process_birth_key(41, 100.0, ticks, platform=platform) == (41, "ticks", ticks)


def test_non_linux_without_ticks_keeps_the_exact_timestamp() -> None:
    assert process_birth_key(41, 100.01, None, platform="darwin") == (41, "timestamp", 100.01)


def test_linux_without_ticks_never_uses_the_timestamp() -> None:
    with pytest.raises(RuntimeError, match="missing Linux start ticks for PID 41"):
        process_birth_key(41, 100.0, None, platform="linux")


@pytest.mark.parametrize("ticks", [0, -1, True])
def test_invalid_ticks_never_form_a_birth_key(ticks: int) -> None:
    with pytest.raises(ValueError, match="native start ticks must be a positive integer"):
        process_birth_key(41, 100.0, ticks, platform="linux")


@pytest.mark.parametrize("case", ["ownership", "loaded-commit"])
def test_native_imports_keep_kernel_observation_local(case: str, tmp_path: Path) -> None:
    # meiva:note
    # A fresh interpreter observes actual package imports, without rewriting modules or globals.
    # The script checks every loaded base module's file and spec against this exact source root.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).with_name("_identity_import_child.py")),
            str(Path(__file__).resolve().parents[3]),
            case,
        ],
        env={"HOME": str(tmp_path), "MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert not observed["pydantic"]
    assert observed["kernel"] == (case == "ownership")
    assert observed["legacy_exports"] == []
