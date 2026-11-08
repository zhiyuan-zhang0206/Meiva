from __future__ import annotations

from pathlib import Path

import pytest

from meizel import allowances
from meizel.state import turn_scoped_config as gate


def _per_agent() -> frozenset[str]:
    return frozenset({"auto_compact_fraction", "reasoning_effort"})


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert gate.main([str(missing)], per_agent_field_names=_per_agent) == 1
    assert str(missing) in capsys.readouterr().err
    assert gate.main([str(good), str(missing)], per_agent_field_names=_per_agent) == 1


def test_explicit_outside_repo_target_scans_cleanly(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    assert gate.main([str(outside)], per_agent_field_names=_per_agent) == 0


# meiva:note
# A test module is skipped by the name the walk reached, not the one the link resolves to: `work.py`
# links to `test_real.py`, and only the reached name decides whether the rule reads it.
def test_a_link_into_the_scan_scope_is_judged_by_the_name_the_scan_reached(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scanned = tmp_path / "base" / "lm"
    scanned.mkdir(parents=True)
    target = scanned / "test_real.py"
    target.write_text("value = settings.agent.reasoning_effort\n", encoding="utf-8")
    (scanned / "work.py").symlink_to(target)
    assert gate.main([], per_agent_field_names=_per_agent, repo_root=tmp_path) == 1
    err = capsys.readouterr().err
    assert err.count("base/lm/test_real.py:1") == 1


def test_a_per_agent_setting_resolved_without_the_agents_overrides_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "bare.py"
    bare.write_text(
        "def f(model):\n"
        '    return resolve_setting("auto_compact_fraction", model=model)\n'
        '\n\ndef g():\n    return get_field("reasoning_effort")\n',
        encoding="utf-8",
    )
    assert gate.main([str(bare)], per_agent_field_names=_per_agent) == 1
    err = capsys.readouterr().err
    assert "bare.py:2" in err
    assert "bare.py:6" in err


def test_the_agents_overrides_and_non_per_agent_settings_pass(tmp_path: Path) -> None:
    fine = tmp_path / "fine.py"
    fine.write_text(
        "def f(model, agent):\n"
        "    resolve_setting(\n"
        '        "auto_compact_fraction", model=model, overrides=agent.overrides\n'
        "    )\n"
        '    return resolve_setting("llm_retry_max_attempts", model=model)\n',
        encoding="utf-8",
    )
    assert gate.main([str(fine)], per_agent_field_names=_per_agent) == 0


# meiva:note
# Which policy a run is judged by is the caller's to say: `policy_root` names the checkout whose
# policy applies and `repo_root` the tree judged. Before they were two names the module read this
# repository's policy at import, so a caller could not judge a synthetic tree by a synthetic policy
# -- and could not tell from the signature which policy was in force.
def test_the_policy_a_run_uses_is_the_one_its_caller_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_root = tmp_path / "policy"
    policy_root.mkdir()
    (policy_root / allowances.POLICY).write_text(
        '[turn-scoped-config]\nscan-dirs = ["lib"]\nextra-files = []\nrule-inputs = []\n'
        "\n[turn-scoped-config.files]\n\n[turn-scoped-config.plugin-mechanism-files]\n",
        encoding="utf-8",
    )
    tree = tmp_path / "tree"
    (tree / "lib").mkdir(parents=True)
    # meiva:note
    # A name the walk does not skip: the rule judges the files beside a test and reports each by the
    # name the scan reached.
    (tree / "lib" / "helper.py").write_text(
        "value = settings.agent.reasoning_effort\n", encoding="utf-8"
    )
    assert (
        gate.main([], per_agent_field_names=_per_agent, repo_root=tree, policy_root=policy_root)
        == 1
    )
    assert "lib/helper.py:1" in capsys.readouterr().err
    # meiva:note
    # The same tree under this repository's policy lies outside every directory it scans.
    assert gate.main([], per_agent_field_names=_per_agent, repo_root=tree) == 0
