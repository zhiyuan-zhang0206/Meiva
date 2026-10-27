from __future__ import annotations

import os
from pathlib import Path

from agent.impersonation import process_host
from base.clock.tests.fakes import system_clock
from base.clock.tests.seeded_rng import seeded_rng


def test_the_hosts_workspaces_live_under_the_home_it_is_given(
    tmp_path: Path, unit_home: Path
) -> None:
    other_home = tmp_path / "other-home"

    host = process_host(
        "macmini",
        home=other_home,
        environ=dict(os.environ),
        rng=seeded_rng(),
        clock=system_clock(),
    )

    assert host.workspaces_root == other_home / "workspaces"
    assert host.workspace(7) == other_home / "workspaces" / "7"
    assert host.workspaces_root != unit_home / "workspaces"


def test_the_hosts_relay_environment_is_the_one_it_is_given(tmp_path: Path) -> None:
    environ = {"MEIVA_HOME": str(tmp_path)}

    host = process_host(
        "macmini", home=tmp_path, environ=environ, rng=seeded_rng(), clock=system_clock()
    )

    assert host.environ == environ
