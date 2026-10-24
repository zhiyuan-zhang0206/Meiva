# meiva:note
# The real parser-bound inspect root must build its required catalog owner and collect it on
# both successful rendering and an unknown-name result. Pure renderer tests cannot catch this.

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from base.host.env.registry import process_environment


@pytest.mark.parametrize(("name", "rc"), [("inspect-root", 0), ("not-installed", 1)])
def test_inspect_handler_builds_the_catalog_and_closes_its_owner(
    unit_home: Path, name: str, rc: int
) -> None:
    # meiva:note
    # The child argv uses the fixed interpreter/script and a parameterized fixture plugin name.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("child_plugins_inspect_root.py")),
            name,
        ],
        env={**process_environment(), "MEIVA_HOME": str(unit_home), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rendered, facts = result.stdout.rsplit("INSPECT_ROOT_RESULT ", 1)
    assert json.loads(facts) == {"rc": rc, "stopped": True}
    if rc == 0:
        assert "plugin inspect-root" in rendered
    else:
        assert "no plugin named 'not-installed'" in result.stderr
