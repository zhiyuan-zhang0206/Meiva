# meiva:link base/cluster/docs/frontend-build.meiva.okf.md

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster import frontend_build
from base.host.proc import run_bounded


def _git(repo: Path, *args: str) -> str:
    result = run_bounded(
        ["git", "-C", str(repo), *args],
        timeout=30.0,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return (result.stdout or "").strip()


def _init_monorepo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    frontend = repo / "ui" / "web"
    (frontend / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("root v1\n")
    (frontend / ".gitignore").write_text("/.next/\n/node_modules/\n*.tsbuildinfo\nnext-env.d.ts\n")
    (frontend / "package.json").write_text('{"name": "meiva-web"}\n')
    (frontend / "src" / "page.tsx").write_text("export default function Page() { return null; }\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo, frontend


def test_a_matching_key_skips_the_build(tmp_path: Path) -> None:
    frontend_build.record(tmp_path, "k")
    decision = frontend_build.plan_frontend_build(
        tmp_path,
        current_key="k",
        recorded=frontend_build.recorded_key(tmp_path),
    )

    assert decision.action == "reuse"
    assert "reusing .next" in decision.message


def test_a_changed_key_rebuilds_and_records(tmp_path: Path) -> None:
    frontend_build.record(tmp_path, "old")
    decision = frontend_build.plan_frontend_build(
        tmp_path,
        current_key="new",
        recorded=frontend_build.recorded_key(tmp_path),
    )
    assert decision.action == "build"
    code = frontend_build.finalize_frontend_build(tmp_path, current_key="new", exit_code=0)

    assert code == 0
    assert frontend_build.recorded_key(tmp_path) == "new"


def test_a_failed_build_records_nothing(tmp_path: Path) -> None:
    frontend_build.record(tmp_path, "old")
    decision = frontend_build.plan_frontend_build(
        tmp_path,
        current_key="new",
        recorded=frontend_build.recorded_key(tmp_path),
    )
    assert decision.action == "build"
    code = frontend_build.finalize_frontend_build(tmp_path, current_key="new", exit_code=3)

    assert code == 3
    assert frontend_build.recorded_key(tmp_path) == "old"


def test_an_unprovable_key_always_builds(tmp_path: Path) -> None:
    decision = frontend_build.plan_frontend_build(
        tmp_path,
        current_key=None,
        recorded=frontend_build.recorded_key(tmp_path),
    )
    assert decision.action == "build"
    code = frontend_build.finalize_frontend_build(tmp_path, current_key=None, exit_code=0)

    assert code == 0
    assert frontend_build.recorded_key(tmp_path) is None


def test_the_key_binds_the_baked_env_and_node() -> None:
    first = frontend_build.key_from_parts(
        "source", "v22.0.0", gateway_port=8000, browser_origins=()
    )
    same = frontend_build.key_from_parts("source", "v22.0.0", gateway_port=8000, browser_origins=())
    other_port = frontend_build.key_from_parts(
        "source", "v22.0.0", gateway_port=8001, browser_origins=()
    )
    other_source = frontend_build.key_from_parts(
        "source2", "v22.0.0", gateway_port=8000, browser_origins=()
    )

    assert first == same
    assert first != other_port
    assert first != other_source


def test_commits_outside_frontend_preserve_build_key_and_reuse_build(tmp_path: Path) -> None:
    repo, frontend = _init_monorepo(tmp_path)
    before_head = _git(repo, "rev-parse", "HEAD")
    before_digest = frontend_build.frontend_source_digest(frontend)
    before_key = frontend_build.build_key(
        frontend, gateway_port=8000, browser_origins=("https://app.example",)
    )
    assert before_key is not None
    frontend_build.record(frontend, before_key)

    (repo / "README.md").write_text("root v2\n")
    (repo / "backend").mkdir()
    (repo / "backend" / "service.py").write_text("VALUE = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "backend change")

    after_head = _git(repo, "rev-parse", "HEAD")
    after_digest = frontend_build.frontend_source_digest(frontend)
    after_key = frontend_build.build_key(
        frontend, gateway_port=8000, browser_origins=("https://app.example",)
    )
    decision = frontend_build.plan_frontend_build(
        frontend, current_key=after_key, recorded=frontend_build.recorded_key(frontend)
    )

    assert after_head != before_head
    assert after_digest == before_digest
    assert after_key == before_key
    assert decision.action == "reuse"


def test_modifying_or_adding_files_inside_frontend_invalidates_build_key(tmp_path: Path) -> None:
    _repo, frontend = _init_monorepo(tmp_path)
    page = frontend / "src" / "page.tsx"
    original = page.read_text()
    base_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert base_key is not None
    frontend_build.record(frontend, base_key)

    page.write_text("export default function Page() { return 'v2'; }\n")
    modified_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert modified_key is not None
    assert modified_key != base_key
    assert (
        frontend_build.plan_frontend_build(
            frontend, current_key=modified_key, recorded=frontend_build.recorded_key(frontend)
        ).action
        == "build"
    )

    page.write_text(original)
    assert frontend_build.build_key(frontend, gateway_port=8000, browser_origins=()) == base_key

    (frontend / "src" / "widget.tsx").write_text("export const W = 1;\n")
    added_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert added_key is not None
    assert added_key not in {base_key, modified_key}
    assert (
        frontend_build.plan_frontend_build(
            frontend, current_key=added_key, recorded=frontend_build.recorded_key(frontend)
        ).action
        == "build"
    )


def test_chmod_symlink_and_deletion_inside_frontend_invalidate_build_key(tmp_path: Path) -> None:
    repo, frontend = _init_monorepo(tmp_path)
    link = frontend / "src" / "linked.ts"
    link.symlink_to("page.tsx")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add symlink")
    page = frontend / "src" / "page.tsx"
    page.chmod(0o644)
    base_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert base_key is not None

    page.chmod(0o755)
    chmod_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert chmod_key != base_key
    page.chmod(0o644)
    assert frontend_build.build_key(frontend, gateway_port=8000, browser_origins=()) == base_key

    link.unlink()
    link.symlink_to("../package.json")
    retargeted_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert retargeted_key != base_key
    link.unlink()
    link.symlink_to("page.tsx")
    assert frontend_build.build_key(frontend, gateway_port=8000, browser_origins=()) == base_key

    page.unlink()
    unstaged_delete_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert unstaged_delete_key != base_key
    _git(repo, "rm", "-f", "ui/web/src/page.tsx")
    _git(repo, "commit", "-m", "remove page")
    committed_delete_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert committed_delete_key not in {base_key, unstaged_delete_key}


def test_gitignored_outputs_inside_frontend_do_not_change_build_key(tmp_path: Path) -> None:
    _repo, frontend = _init_monorepo(tmp_path)
    before_digest = frontend_build.frontend_source_digest(frontend)
    before_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    assert before_key is not None
    frontend_build.record(frontend, before_key)

    (frontend / ".next" / "cache" / "turbopack").mkdir(parents=True, exist_ok=True)
    (frontend / ".next" / "cache" / "turbopack" / "meta").write_bytes(b"cached")
    (frontend / "node_modules" / "next").mkdir(parents=True)
    (frontend / "node_modules" / "next" / "index.js").write_text("module.exports = {};\n")
    (frontend / "tsconfig.tsbuildinfo").write_text('{"version": "5.9"}\n')
    (frontend / "next-env.d.ts").write_text("/// <reference types='next' />\n")

    after_digest = frontend_build.frontend_source_digest(frontend)
    after_key = frontend_build.build_key(frontend, gateway_port=8000, browser_origins=())
    decision = frontend_build.plan_frontend_build(
        frontend, current_key=after_key, recorded=frontend_build.recorded_key(frontend)
    )

    assert after_digest == before_digest
    assert after_key == before_key
    assert decision.action == "reuse"


def test_non_git_or_missing_directory_fails_closed_to_build(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        frontend_build.frontend_source_digest(missing)
    assert frontend_build.build_key(missing, gateway_port=8000, browser_origins=()) is None

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="git ls-files failed"):
        frontend_build.frontend_source_digest(plain)
    assert frontend_build.build_key(plain, gateway_port=8000, browser_origins=()) is None
