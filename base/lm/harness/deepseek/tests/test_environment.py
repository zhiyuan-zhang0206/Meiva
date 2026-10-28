from __future__ import annotations

from pathlib import Path

from base.lm.harness.deepseek.environment import environment


def test_the_runtime_home_is_set_beside_what_the_session_was_handed(tmp_path: Path) -> None:
    merged = environment({"PATH": "/bin", "LANG": "C"}, dsh_home=tmp_path / ".dsh", overrides={})
    assert merged == {"PATH": "/bin", "LANG": "C", "DSH_HOME": str(tmp_path / ".dsh")}


def test_the_configured_overrides_win_over_both_other_sources(tmp_path: Path) -> None:
    merged = environment(
        {"DSH_HOME": "/elsewhere", "PATH": "/bin"},
        dsh_home=tmp_path / ".dsh",
        overrides={"DSH_HOME": "/override", "PATH": "/opt/bin"},
    )
    assert merged == {"DSH_HOME": "/override", "PATH": "/opt/bin"}


def test_the_environment_it_is_given_is_not_modified(tmp_path: Path) -> None:
    given = {"PATH": "/bin"}
    overrides = {"LANG": "C"}
    environment(given, dsh_home=tmp_path, overrides=overrides)
    assert given == {"PATH": "/bin"}
    assert overrides == {"LANG": "C"}
