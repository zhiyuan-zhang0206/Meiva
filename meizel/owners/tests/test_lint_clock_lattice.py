"""`meizel/owners/clock_lattice.py` — the lattice-vocabulary placement invariant.

A module-level constant whose name carries lattice vocabulary (STALL / GRACE /
REAP / BUDGET / WEDGED / NO_PROGRESS / LAUNCH_CONFIRM / LEASE_TTL / LEASE_RENEW /
SCAN_INTERVAL) must live in a lattice family module, be an alias of a registered clock, or carry an explicit exemption.
A bare `_SOME_REAP_GRACE_S = 100` in a new module is the 2026-07-30 spawn
incident's seedling — the name reads as part of the lattice while nothing knows
its neighbours.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from meizel.owners import clock_lattice
from meizel.owners import clock_lattice_cli as _lint
from meizel.owners.clock_lattice_cli import SCAN_DIRS as _SCAN_DIRS

# Untyped fixtures and helper calls throughout: the call-site rules stay at warning for this file.
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_CLOCKS = frozenset({"NO_PROGRESS_TIMEOUT_S"})
_FAMILY = ("base/deploy/timing.py", "base/native_process/stop_timing.py")
_INDEPENDENT = {
    ("base/host/proc.py", "_TERMINATE_GRACE_S"): "independent: TERM->KILL ladder, no neighbour",
}


# meiva:note
# The lattice a test hands the rule: a declaration of this fixture tree, never the repository's.
def _lattice(
    *,
    family: tuple[str, ...] = _FAMILY,
    independent: dict[tuple[str, str], str] | None = None,
    registered: frozenset[str] = _TEST_CLOCKS,
) -> clock_lattice.Lattice:
    return clock_lattice.Lattice(
        family, _INDEPENDENT if independent is None else independent, registered
    )


_TEST_LATTICE = _lattice()


@pytest.fixture()
def scan_tmp(tmp_path: Path) -> Path:
    """Scratch tree root so fixtures never touch the real repo."""
    return tmp_path


def _write(scan_tmp: Path, rel: str, body: str) -> Path:
    p = scan_tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _errors(scan_tmp: Path, rel: str, body: str) -> list[str]:
    p = _write(scan_tmp, rel, body)
    return _lint.scan_file(p, repo_root=scan_tmp, lattice=_TEST_LATTICE)


def test_bare_lattice_clock_outside_family_is_rejected(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "ops/somewhere.py",
        """
        _SOME_REAP_GRACE_S = 100.0
        """,
    )
    assert len(errs) == 1
    assert "ops/somewhere.py:2" in errs[0]
    assert "_SOME_REAP_GRACE_S" in errs[0]


def test_family_module_definition_is_allowed(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "base/deploy/timing.py",
        """
        CONTROLLER_SCAN_INTERVAL_S = 30.0
        """,
    )
    assert errs == []


def test_alias_of_registered_clock_is_allowed(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "ops/controllers/stalled_rollout.py",
        """
        _ROLLOUT_STALL_TIMEOUT_S: float = NO_PROGRESS_TIMEOUT_S
        """,
    )
    assert errs == []


def test_exempt_clock_is_allowed(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "base/host/proc.py",
        """
        _TERMINATE_GRACE_S = 3.0
        """,
    )
    assert errs == []


def test_independent_clock_without_lattice_vocabulary_is_allowed(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "meiva/mcps/_oauth.py",
        """
        OAUTH_FLOW_TIMEOUT_S = 600.0
        """,
    )
    assert errs == []


def test_settings_field_in_class_body_is_not_scanned(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "base/config/domains/gateway.py",
        """
        class GatewaySettings(EnvSettings):
            launch_confirm_timeout_seconds: float = Field(
                default=45.0, alias="MEIVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS"
            )
        """,
    )
    assert errs == []


def test_unknown_clock_alias_is_rejected(scan_tmp) -> None:
    """An alias must reference a clock that is actually registered — a typo'd
    name must not ride the alias rule."""
    errs = _errors(
        scan_tmp,
        "ops/nowhere.py",
        """
        _MY_STALL_TIMEOUT_S: float = NO_PROGRES_TIMEOUT_S
        """,
    )
    assert len(errs) == 1


def test_explicit_missing_target_is_an_error(
    scan_tmp: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd explicit path must fail the gate, not die in a bare traceback."""
    good = _write(scan_tmp, "ok.py", "value = 1\n")
    missing = tmp_path / "typo.py"
    assert (
        _lint.main([str(missing)], scan_dirs=_SCAN_DIRS, repo_root=scan_tmp, lattice=_TEST_LATTICE)
        == 1
    )
    assert str(missing) in capsys.readouterr().err
    assert (
        _lint.main(
            [str(good), str(missing)],
            scan_dirs=_SCAN_DIRS,
            repo_root=scan_tmp,
            lattice=_TEST_LATTICE,
        )
        == 1
    )


def test_directory_with_dangling_symlink_member_is_skipped(scan_tmp: Path) -> None:
    """A broken *.py symlink inside an explicit directory must be skipped like
    any unreadable entry — the scan must not crash on it."""
    pkg = scan_tmp / "pkg"
    pkg.mkdir()
    (pkg / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (pkg / "dangling.py").symlink_to(pkg / "missing.py")
    assert (
        _lint.main([str(pkg)], scan_dirs=_SCAN_DIRS, repo_root=scan_tmp, lattice=_TEST_LATTICE) == 0
    )


def test_explicit_outside_repo_and_relative_targets_are_scanned(
    scan_tmp: Path,
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A path outside the repo, or given relative to the cwd, must be scanned
    for real: the lattice violation in it is reported (exit 1) rather than
    tolerated as a clean scan, and the scan must not die on the repo-relative
    prefix computation."""
    outside = tmp_path_factory.mktemp("outside")
    bad = outside / "bad.py"
    bad.write_text("_MY_STALL_TIMEOUT_S = 5\n", encoding="utf-8")
    assert (
        _lint.main([str(bad)], scan_dirs=_SCAN_DIRS, repo_root=scan_tmp, lattice=_TEST_LATTICE) == 1
    )
    prev_cwd = Path.cwd()
    try:
        os.chdir(outside)
        assert (
            _lint.main(["bad.py"], scan_dirs=_SCAN_DIRS, repo_root=scan_tmp, lattice=_TEST_LATTICE)
            == 1
        )
    finally:
        os.chdir(prev_cwd)
    assert "bad.py" in capsys.readouterr().err


def test_directory_with_non_utf8_member_is_skipped(
    scan_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-UTF-8 *.py member of an explicit directory is skipped like any
    unreadable entry — the scan must not crash on it, and a violating sibling
    file is still reported."""
    pkg = scan_tmp / "pkg"
    pkg.mkdir()
    (pkg / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (pkg / "bad_utf8.py").write_bytes(b"\xff\xfe\x00bad")
    assert (
        _lint.main([str(pkg)], scan_dirs=_SCAN_DIRS, repo_root=scan_tmp, lattice=_TEST_LATTICE) == 0
    )
    (pkg / "viol.py").write_text("_MY_STALL_TIMEOUT_S = 5\n", encoding="utf-8")
    assert (
        _lint.main([str(pkg)], scan_dirs=_SCAN_DIRS, repo_root=scan_tmp, lattice=_TEST_LATTICE) == 1
    )
    assert "_MY_STALL_TIMEOUT_S" in capsys.readouterr().err


# --- stale allowlist entries fail a full scan ---------------------------------


def test_real_allowlists_are_current() -> None:
    assert _lint.stale_entries(_REPO_ROOT, _lint.lattice_of(_REPO_ROOT)) == []


def test_exemption_for_a_deleted_file_is_stale() -> None:
    errs = clock_lattice.stale_allowlist_entries(
        set(),
        {},
        lattice=_lattice(family=(), independent={("base/gone.py", "_REAP_S"): "why"}),
    )
    assert len(errs) == 1
    assert "base/gone.py" in errs[0] and "_REAP_S" in errs[0]


def test_exemption_for_a_removed_constant_is_stale(scan_tmp: Path) -> None:
    path = _write(scan_tmp, "base/host/proc.py", "_OTHER_GRACE_S = 1.0\n")
    errs = clock_lattice.stale_allowlist_entries(
        {"base/host/proc.py"},
        {"base/host/proc.py": path.read_text(encoding="utf-8")},
        lattice=_lattice(
            family=(), independent={("base/host/proc.py", "_TERMINATE_GRACE_S"): "why"}
        ),
    )
    assert len(errs) == 1
    assert "no module-level _TERMINATE_GRACE_S" in errs[0]


def test_live_exemption_and_family_module_are_current(scan_tmp: Path) -> None:
    _write(scan_tmp, "base/host/proc.py", "_TERMINATE_GRACE_S: float = 1.0\n")
    _write(scan_tmp, "base/deploy/timing.py", "CLOCKS = {}\n")
    assert (
        clock_lattice.stale_allowlist_entries(
            {"base/host/proc.py", "base/deploy/timing.py", "base/native_process/stop_timing.py"},
            {"base/host/proc.py": "_TERMINATE_GRACE_S: float = 1.0\n"},
            lattice=_lattice(),
        )
        == []
    )


def test_full_scan_fails_on_a_stale_family_module(
    scan_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the argument-free full scan (pre-commit, CI) judges the allowlists."""
    for scan_dir in _SCAN_DIRS:
        (scan_tmp / scan_dir).mkdir(parents=True, exist_ok=True)
    assert (
        _lint.main(
            [],
            scan_dirs=_SCAN_DIRS,
            repo_root=scan_tmp,
            lattice=_lattice(family=("base/retired_timing.py",), independent={}),
        )
        == 1
    )
    assert "stale FAMILY_MODULES entry" in capsys.readouterr().err
    ok = _write(scan_tmp, "ok.py", "value = 1\n")
    assert (
        _lint.main(
            [str(ok)],
            scan_dirs=_SCAN_DIRS,
            repo_root=scan_tmp,
            lattice=_lattice(family=("base/retired_timing.py",), independent={}),
        )
        == 0
    )


# ── values bound to a clock name ──────────────────────────────────────────────


def test_a_dataclass_default_that_is_a_literal_is_a_definition(scan_tmp) -> None:
    errs = _errors(
        scan_tmp,
        "services/derived/thing.py",
        """
        from dataclasses import dataclass


        @dataclass(frozen=True)
        class ThingConfig:
            batch_size: int = 32
            stall_timeout_s: float = 5.0
            reap_grace_s: float
            wedged_after_s: float | None = None
        """,
    )
    assert len(errs) == 1
    assert "stall_timeout_s" in errs[0] and "services/derived/thing.py:8" in errs[0]


@pytest.mark.parametrize(
    "body",
    [
        "def go(cfg):\n    return cfg(startup_grace_s=30)\n",
        "def go(cfg):\n    return cfg(reap_grace_s=5 * 60)\n",
        "def go(box):\n    startup_grace_s = 2.5\n    box.stall_timeout_s = -1.0\n",
        "class Box:\n    grace_s = 7\n",
        "grace_s: float = 7.0\n",
    ],
)
def test_every_way_of_binding_a_literal_counts(scan_tmp, body: str) -> None:
    assert _errors(scan_tmp, "ops/bound.py", body), body


@pytest.mark.parametrize(
    "body",
    [
        "def go(cfg, config):\n    return cfg(startup_grace_s=config.startup_grace_s)\n",
        "from base.deploy.timing import TERMINATE_GRACE_S\n\ndef go(cfg):\n    return cfg(grace_s=TERMINATE_GRACE_S)\n",
        "def go(cfg):\n    return cfg(grace_s=None)\n",
        "def go(cfg):\n    return cfg(grace_s=cfg.default() + 1)\n",
        "class Cfg:\n    grace_s: float\n",
        "def go(cfg):\n    return cfg(install_count=3, installation=9, breakfast_s=1)\n",
        "def go(cfg):\n    return cfg(grace_s='soon', stall_s=True)\n",
    ],
)
def test_a_reference_a_missing_default_or_another_word_defines_nothing(scan_tmp, body: str) -> None:
    assert _errors(scan_tmp, "ops/not_bound.py", body) == [], body


def test_a_family_module_may_bind_literals(scan_tmp) -> None:
    body = "from dataclasses import dataclass\n\n@dataclass\nclass StopTimingConfig:\n    cancel_unwind_grace_s: float = 5.0\n"
    assert _errors(scan_tmp, "base/native_process/stop_timing.py", body) == []


def test_an_audited_exemption_lets_a_binding_stand(scan_tmp: Path) -> None:
    body = "def go(cfg):\n    return cfg(claude_thinking_budget_tokens=0)\n"
    path = _write(scan_tmp, "ops/z.py", body)
    assert _lint.scan_file(path, repo_root=scan_tmp, lattice=_lattice(independent={})) != []
    reason = {("ops/z.py", "claude_thinking_budget_tokens"): "a token budget, not a time value"}
    assert _lint.scan_file(path, repo_root=scan_tmp, lattice=_lattice(independent=reason)) == []


def test_an_exemption_that_binds_nothing_any_more_is_stale(scan_tmp: Path) -> None:
    path = _write(scan_tmp, "ops/z.py", "def go(cfg):\n    return cfg(limit=3)\n")
    gone = {("ops/z.py", "claude_thinking_budget_tokens"): "a token budget, not a time value"}
    stale = [
        m
        for m in clock_lattice.stale_allowlist_entries(
            {"ops/z.py"},
            {"ops/z.py": path.read_text(encoding="utf-8")},
            lattice=_lattice(family=(), independent=gone),
        )
        if "claude_thinking" in m
    ]
    assert len(stale) == 1 and "drop the exemption" in stale[0]


def test_a_family_module_that_is_not_utf8_still_exists(scan_tmp: Path) -> None:
    binary = scan_tmp / "base" / "binary.py"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xff\xfe not utf-8")
    errs = clock_lattice.stale_allowlist_entries(
        {"base/binary.py"},
        {},
        lattice=_lattice(family=("base/binary.py", "base/gone.py"), independent={}),
    )
    assert errs == ["base/gone.py: stale FAMILY_MODULES entry — the file no longer exists"]


# meiva:note
# The rule takes its scan directories from the shell, so the module itself must not report a pass
# when something still starts it: a stale local hook or script would read green from an empty run.
def test_the_rule_module_refuses_to_start_itself() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "meizel.owners.clock_lattice"],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert result.returncode != 0
    assert "clock_lattice_cli" in result.stderr


# meiva:note
# A run admits the checkout once, so the two things that could go wrong with that
# reuse are pinned end to end: a violation created between two runs is reported by
# the second (the reuse does not outlive the run), and a touch of an unrelated file
# does not change the verdict (the file set is re-read, not remembered).
# meiva:link meizel/docs/lint/lint-common-admission.meiva.okf.md
def _git_run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603


def _admitted_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in ("pkg/clean.py", "pkg/family.py", "docs/notes.md"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n", encoding="utf-8")
    _git_run(root, "init", "-q")
    _git_run(root, "add", ".")
    return root


def _run_lint(root: Path) -> int:
    return _lint.main(
        [],
        scan_dirs=("pkg",),
        repo_root=root,
        lattice=_lattice(family=("pkg/family.py",), independent={}),
    )


def test_a_violation_created_between_two_runs_is_reported(tmp_path: Path, capsys) -> None:
    root = _admitted_root(tmp_path)
    assert _run_lint(root) == 0
    capsys.readouterr()
    offender = root / "pkg/bad.py"
    offender.write_text("_SOME_REAP_GRACE_S = 1.0\n", encoding="utf-8")
    _git_run(root, "add", "pkg/bad.py")
    assert _run_lint(root) == 1
    assert "pkg/bad.py" in capsys.readouterr().err


def test_a_touched_unrelated_file_keeps_the_verdict(tmp_path: Path, capsys) -> None:
    root = _admitted_root(tmp_path)
    assert _run_lint(root) == 0
    capsys.readouterr()
    (root / "docs/notes.md").touch()
    assert _run_lint(root) == 0
    violations = [
        line
        for line in capsys.readouterr().err.splitlines()
        if not line.startswith("lint admission:")
    ]
    assert violations == []
