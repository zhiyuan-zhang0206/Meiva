from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from cli.fleet_update.plan.release_target import (
    ReleaseCertificate,
    format_tag_message,
)
from scripts.host_ops.release import base as release_base


def _cert(
    *,
    version: str = "v2026.11.01.120000",
    sha: str = "a" * 40,
    hotfix_of: str | None = None,
) -> ReleaseCertificate:
    return ReleaseCertificate(
        version=version,
        sha=sha,
        source_ref="refs/heads/main",
        gate_run_id="1",
        gate_run_url="https://github.com/o/r/actions/runs/1",
        certified_at="2026-11-01T12:00:00Z",
        hotfix_of=hotfix_of,
    )


def _ancestor_of(edges: dict[str, set[str]]) -> Callable[[str, str], bool]:
    def check(ancestor: str, target: str) -> bool:
        seen: set[str] = set()
        stack = [target]
        while stack:
            node = stack.pop()
            if node == ancestor:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges.get(node, set()))
        return False

    return check


def _git(repo: Path, *arguments: str) -> str:
    done = subprocess.run(  # noqa: S603
        ("git", "-C", str(repo), *arguments), check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_target_descending_from_a_certified_release_is_allowed() -> None:
    gated = "a" * 40
    deployed = "b" * 40
    verdict = release_base.verify_release_base(
        deployed,
        [_cert(sha=gated)],
        _ancestor_of({deployed: {gated}}),
    )

    assert verdict.ok
    assert verdict.reason == "descends-from-release/v2026.11.01.120000"
    assert verdict.base is not None and verdict.base.sha == gated


def test_no_provenance_refuses_unless_bootstrap_is_explicit() -> None:
    denied = release_base.verify_release_base("b" * 40, [], _ancestor_of({}))
    assert not denied.ok and denied.reason == "no-provenance"

    bootstrapped = release_base.verify_release_base(
        "b" * 40, [], _ancestor_of({}), bootstrap_allowed=True
    )
    assert bootstrapped.ok and bootstrapped.reason == "bootstrap-no-release-history"


def test_non_ancestor_refuses_even_when_bootstrap_flag_is_passed() -> None:
    verdict = release_base.verify_release_base(
        "c" * 40,
        [_cert(sha="a" * 40)],
        _ancestor_of({"c" * 40: {"b" * 40}}),
        bootstrap_allowed=True,
    )
    assert not verdict.ok and verdict.reason == "no-gated-ancestor"


def test_highest_reachable_calver_including_hotfix_wins() -> None:
    base_sha = "a" * 40
    hf_sha = "b" * 40
    tip_sha = "c" * 40
    older = _cert(version="v2026.11.01.120000", sha=base_sha)
    hotfix = _cert(
        version="v2026.11.01.120000-hf1",
        sha=hf_sha,
        hotfix_of="v2026.11.01.120000",
    )
    verdict = release_base.verify_release_base(
        tip_sha,
        [older, hotfix],
        _ancestor_of({tip_sha: {hf_sha}, hf_sha: {base_sha}}),
    )
    assert verdict.ok
    assert verdict.base == hotfix


def test_load_certified_releases_ignores_foreign_tags_and_rejects_forged_calver_tags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, first = _repository(tmp_path)
    _git(repo, "tag", "v0.1.0", first)
    _git(repo, "tag", "m2-full", first)
    assert release_base.has_release_history(repo) is False
    assert release_base.load_certified_releases(repo) == []

    cert = _cert(version="v2026.11.01.120000", sha=first)
    _git(repo, "tag", "-a", cert.version, "-m", format_tag_message(cert), first)
    assert release_base.has_release_history(repo) is True
    assert release_base.load_certified_releases(repo) == [cert]

    (repo / "README.md").write_text("next\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "next")
    second = _git(repo, "rev-parse", "HEAD")

    assert release_base.main(["--repo", str(repo), "--target", second]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["base_version"] == "v2026.11.01.120000"

    _git(repo, "tag", "v2026.11.01.130000", second)
    with pytest.raises(ValueError, match="not an annotated tag"):
        release_base.load_certified_releases(repo)
    assert release_base.main(["--repo", str(repo), "--target", second]) == 1
    bad_payload = json.loads(capsys.readouterr().out)
    assert bad_payload["ok"] is False and bad_payload["reason"] == "invalid-certificate"
