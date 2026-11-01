from __future__ import annotations

from pathlib import Path

from scripts.lint.locks import package_lock as gate

_CANONICAL_TARBALL = "https://registry.npmjs.org/example/-/example-1.0.0.tgz"


def _lock(path: Path, *, tarball: str | None = None) -> Path:
    resolution = (
        f"{{integrity: sha512-abc, tarball: {tarball}}}"
        if tarball is not None
        else "{integrity: sha512-abc}"
    )
    path.write_text(
        "lockfileVersion: '9.0'\n\n"
        "importers:\n"
        "  .:\n"
        "    dependencies:\n"
        "      example:\n"
        "        specifier: ^1.0.0\n"
        "        version: 1.0.0\n\n"
        "packages:\n"
        "  example@1.0.0:\n"
        f"    resolution: {resolution}\n",
        encoding="utf-8",
    )
    return path


def test_rejects_a_mirror_tarball_url(tmp_path: Path) -> None:
    path = _lock(
        tmp_path / "pnpm-lock.yaml",
        tarball="https://registry.npmmirror.com/example/-/example-1.0.0.tgz",
    )
    errors = gate.violations(path)
    assert len(errors) == 1
    assert "registry.npmjs.org" in errors[0]


def test_accepts_canonical_and_tarballless_entries(tmp_path: Path) -> None:
    path = _lock(tmp_path / "pnpm-lock.yaml", tarball=_CANONICAL_TARBALL)
    assert gate.violations(path) == []
    path = _lock(tmp_path / "pnpm-lock.yaml", tarball=None)
    assert gate.violations(path) == []


def test_rejects_missing_or_corrupt_lockfile(tmp_path: Path) -> None:
    assert gate.violations(tmp_path / "missing.yaml") == ["missing lockfile"]
    bad = tmp_path / "pnpm-lock.yaml"
    bad.write_text("lockfileVersion: '6.0'\n", encoding="utf-8")
    assert gate.violations(bad) == ["expected lockfileVersion: '9.0' header"]


def test_workspace_rejects_registry_override_and_obsolete_npm_files(tmp_path: Path) -> None:
    fe = tmp_path / "ui" / "web"
    fe.mkdir(parents=True)
    _lock(fe / "pnpm-lock.yaml")
    (fe / "pnpm-workspace.yaml").write_text(
        "nodeLinker: hoisted\nregistry: https://registry.npmmirror.com/\n",
        encoding="utf-8",
    )
    (fe / "package-lock.json").write_text("{}", encoding="utf-8")
    errors = gate.repo_violations(tmp_path)
    assert any("registry key" in err for err in errors)
    assert any("package-lock.json" in err for err in errors)
