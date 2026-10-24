from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_CHILD = Path(__file__).with_name("import_surface_child.py")
_REPO = Path(__file__).resolve().parents[4]


# meiva:note
# Launch only the repository helper with the two fixed surface choices. A fresh
# child prevents earlier harness imports from hiding assembly initialization.
@pytest.mark.parametrize("surface", ["model", "public"])
def test_declaration_imports_do_not_initialize_application_assembly(surface: str) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), surface],
        cwd=_REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["same_identity"] is True
    assert observed["loaded_observers"] == []
