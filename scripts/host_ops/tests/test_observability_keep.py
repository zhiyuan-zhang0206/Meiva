from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from scripts.host_ops import observability_keep as keep

_REPO = Path(__file__).resolve().parents[3]

_CONSUMERS = (
    "agent/",
    "gateway/",
    "meiva/",
    "ops/",
    "services/wake/",
    "services/backup/",
    "services/derived/",
    "services/upkeep/",
    "cli/commands/cluster/",
    "cli/commands/management/",
    "cli/commands/lifecycle/start/",
    "cli/commands/lifecycle/status/",
    "cli/commands/lifecycle/stop/",
    "cli/commands/probe.py",
    "cli/database/",
    "cli/fleet_update/",
    "cli/parsers/",
    "cli/startup/",
    "base/agents/",
    "base/api_contracts/",
    "base/events/live/",
    "base/events/reads/",
    "base/host/resource/",
    "base/lm/",
    "base/config/domains/display.py",
    "base/config/_lite.py",
    "base/config/config_registry.py",
    "base/deploy/",
    "meiva_builtins/",
    "db/",
    "migrations/",
    "scripts/",
    "docs/",
    ".agents/",
    ".github/",
    ".env.example",
    ".pre-commit-config.yaml",
    "pyproject.toml",
    "meizel.",
)
_NAMES = re.compile(
    r"\b(loki|prometheus|grafana|otel-?collector|otelcol|lgtm)\b|\btempo\b", re.IGNORECASE
)
_EXCLUDED = re.compile(r"(^|/)(tests?|docs)/|\.md$|^ui/|^future/|^assets/|^tests/")


def _tracked_files() -> list[str]:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(_REPO), "ls-files", "-z"], capture_output=True, text=True, check=True
    )
    return [path for path in done.stdout.split("\0") if path]


def test_every_file_naming_a_backend_is_a_restart_reason_or_a_reviewed_consumer() -> None:
    unreviewed: list[str] = []
    for path in _tracked_files():
        if _EXCLUDED.search(path) or keep.reaches_stack(path):
            continue
        if path.startswith(_CONSUMERS):
            continue
        try:
            text = (_REPO / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _NAMES.search(text):
            unreviewed.append(path)
    assert unreviewed == [], (
        "these files name an observability backend but are neither restart reasons "
        "(RESTART_PREFIXES) nor reviewed consumers (_CONSUMERS): " + ", ".join(unreviewed)
    )


@pytest.mark.parametrize(
    "path",
    [
        "deploy/lgtm/native/config/loki.yaml",
        "deploy/lgtm/native/versions.yml",
        "deploy/lgtm/config/grafana/provisioning/alerting/rules.yml",
        "deploy/lgtm/config/grafana/provisioning/dashboards/meiva.json",
        "deploy/otel-collector/otel-collector.yaml",
        "cli/commands/observability/lgtm_native.py",
        "cli/commands/observability/plans/collector/render.py",
        "base/telemetry/metrics/core/panels.py",
        "base/telemetry/lgtm_local.py",
        "base/config/domains/observability/tempo.py",
        "base/paths/home.py",
        "base/native_process/stop_timing.py",
        "services/supervision/roster/composition.py",
        "services/supervision/healthchecks/lgtm.py",
        "services/supervision/meiva_root/supervisor.py",
        "gateway/alerts/router.py",
        "meiva_builtins/plugins/meiva_memory/metrics.py",
        "base/events/declarations/observability.py",
    ],
)
def test_a_path_that_can_reach_the_stack_is_a_restart_reason(path: str) -> None:
    advice = keep.advise([path])
    assert not advice.keep and advice.hits == (path,)


@pytest.mark.parametrize(
    "path",
    [
        "ui/web/src/app/home-view.tsx",
        "agent/graph/exec/output.py",
        "cli/fleet_update/down.py",
        "schedules/jobs/debt_sweep.py",
        "migrations/2026/11/02/x.sql",
        "docs/conventions/runbook.md",
        "base/agents/history/checkpoint.py",
        "uv.lock",
    ],
)
def test_a_path_with_no_route_to_the_stack_keeps_it(path: str) -> None:
    assert keep.advise([path]).keep


def test_one_reason_among_many_unrelated_paths_is_enough_and_is_named() -> None:
    advice = keep.advise(["ui/web/a.tsx", "deploy/lgtm/native/config/tempo.yaml", "agent/x.py"])
    assert advice.render() == (
        "restart: 1 changed path(s) can reach the observability stack\n"
        "  deploy/lgtm/native/config/tempo.yaml"
    )


def test_a_keep_verdict_names_exactly_the_five_units() -> None:
    assert keep.advise([]).render() == (
        "keep: --keep-service loki --keep-service prometheus --keep-service tempo "
        "--keep-service grafana --keep-service otel-collector"
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": "/usr/bin:/bin",
        },
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, body in files.items():
        target = repo / name
        if body is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def test_the_command_reads_the_real_diff_including_deletions_and_renames(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q")
    base = _commit(
        tmp_path,
        {"agent/a.py": "1\n", "deploy/lgtm/old.yaml": "x\n", "ui/web/p.tsx": "1\n"},
        "base",
    )
    ui_only = _commit(tmp_path, {"ui/web/p.tsx": "2\n", "agent/a.py": "2\n"}, "ui")
    assert keep.main([base, ui_only, "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().out.startswith("keep: --keep-service loki")

    deleted = _commit(tmp_path, {"deploy/lgtm/old.yaml": None}, "delete")
    assert keep.main([ui_only, deleted, "--repo", str(tmp_path)]) == 1
    assert "  deploy/lgtm/old.yaml" in capsys.readouterr().out

    (tmp_path / "deploy/lgtm").mkdir(parents=True, exist_ok=True)
    kept = _commit(tmp_path, {"deploy/lgtm/rules.yaml": "r\n"}, "add")
    moved = _commit(tmp_path, {"deploy/lgtm/rules.yaml": None, "agent/rules.yaml": "r\n"}, "move")
    assert keep.main([kept, moved, "--repo", str(tmp_path)]) == 1
    assert "  deploy/lgtm/rules.yaml" in capsys.readouterr().out


def test_an_unresolvable_commit_is_no_verdict_and_never_a_keep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q")
    head = _commit(tmp_path, {"a.txt": "1\n"}, "base")
    assert keep.main([head, "0" * 40, "--repo", str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert out.startswith("restart: no verdict") and "keep:" not in out


@pytest.mark.parametrize("argv", [[], ["only-one"], ["a", "b", "c"], ["a", "b", "--repo"]])
def test_a_malformed_command_line_is_a_usage_error(argv: list[str]) -> None:
    assert keep.main(argv) == 2
