# meiva:note
# The WAL-G domain belongs to the CLI owner; a gateway-profile owner never carries it.
#
# Each owner is a fresh interpreter over a home of its own, with its profile in the process
# environment the way a launcher sets it.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_CHILD = Path(__file__).with_name("child_walg.py")


# meiva:note
# The child is a fixed script of this directory started with the interpreter's own argv,
# so the subprocess suppression is the harness's.
def _owner(tmp_path: Path, profile: str | None) -> dict[str, object]:
    home = tmp_path / ("home-" + (profile or "cli"))
    home.mkdir()
    (home / ".env").write_text(
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        "MEIVA_DB_URL=postgresql://test:test@127.0.0.1:1/initial\n"
        "MEIVA_REDIS_URL=redis://127.0.0.1:1/0\n",
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MEIVA_") or key == "MEIVA_CONFIG_FETCH"
    }
    env["MEIVA_HOME"] = str(home)
    if profile is not None:
        env["MEIVA_PROCESS_PROFILE"] = profile
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), "profile", str(tmp_path / "walg.json")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(proc.stdout)


def test_the_cli_owner_supplies_its_configured_path_and_stays_independent_of_other_owners(
    tmp_path: Path,
) -> None:
    facts = _owner(tmp_path, None)

    assert facts["declared"] is True
    assert facts["owner_reader"] == str(tmp_path / "walg.json")
    assert facts["other_declared"] is True


def test_a_gateway_owner_excludes_the_walg_domain_before_and_after_it_goes_eager(
    tmp_path: Path,
) -> None:
    facts = _owner(tmp_path, "gateway")

    assert facts["declared"] is False and facts["readable"] is False
    assert facts["declared_after_eager"] is False and facts["readable_after_eager"] is False
