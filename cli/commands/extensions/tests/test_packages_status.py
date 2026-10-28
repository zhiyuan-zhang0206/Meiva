"""`meiva packages status` — the read-only surface over registry schema v2.

Host version + channel line + per-package channel/policy/applied-rev/last
result/declared range, in text and `--json` (tasks #2915 / #3267).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.config import ConfigBoot
from base.packages.extensions import install_registry as reg
from base.paths import meiva_home
from cli.commands.extensions.packages import cmd_packages_status


def _register(name: str, **kw: object) -> reg.InstalledPackage:
    pkg = reg.InstalledPackage(name=name, type=kw.pop("type", "skill"), **kw)  # pyright: ignore[reportArgumentType]
    reg.register(pkg, home=meiva_home())
    return pkg


def test_empty_registry_prints_host_and_no_packages(
    process_config: ConfigBoot, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cmd_packages_status(
            config=process_config,
            now=datetime.now(UTC),
            home=unit_home,
            user_home=unit_home,
        )
        == 0
    )
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "registry v2" in out
    assert "(no tracked packages)" in out
    assert "channel core: not fetched yet" in out


def test_text_table_shows_resolved_policy_per_source_class(
    process_config: ConfigBoot, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register("repo-skill", origin="repo", origin_path="/x/meiva_builtins/skills/repo-skill")
    _register("git-skill", origin="user", source="https://x/git-skill")
    _register("local-skill", origin="user")

    assert (
        cmd_packages_status(
            config=process_config,
            now=datetime.now(UTC),
            home=unit_home,
            user_home=unit_home,
        )
        == 0
    )
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.startswith("  ") and "skill" in ln}
    assert (
        "core" in lines["repo-skill"]
        and "auto" in lines["repo-skill"]
        and "24h" in lines["repo-skill"]
    )
    assert "git" in lines["git-skill"] and "auto" in lines["git-skill"]
    assert "off" in lines["local-skill"]


def test_json_output_carries_host_channels_and_packages(
    process_config: ConfigBoot, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register("repo-skill", origin="repo", origin_path="/x/meiva_builtins/skills/repo-skill")

    assert (
        cmd_packages_status(
            config=process_config,
            json_output=True,
            now=datetime.now(UTC),
            home=unit_home,
            user_home=unit_home,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    assert payload["host"]["registry_schema"] == 2
    assert "+g" in payload["host"]["display"]
    assert payload["channels"] == {}
    (row,) = payload["packages"]
    assert row["name"] == "repo-skill"
    assert row["channel"] == "core"
    assert row["mode"] == "auto"
    assert row["interval_seconds"] == 86400
    assert row["applied_rev"] is None
    assert row["manifest"] is None


def test_invalid_manifest_reported_not_crashed(
    process_config: ConfigBoot, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register("broken", origin="repo", origin_path="/x/meiva_builtins/skills/broken")
    manifest = unit_home / "skills" / "broken" / "meiva-plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{ not json", encoding="utf-8")

    assert (
        cmd_packages_status(
            config=process_config,
            json_output=True,
            now=datetime.now(UTC),
            home=unit_home,
            user_home=unit_home,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    (row,) = payload["packages"]
    assert row["manifest"] is None
    assert "not valid JSON" in row["manifest_error"]


def test_declared_range_displayed(
    process_config: ConfigBoot, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register("ranged", origin="repo", origin_path="/x/meiva_builtins/skills/ranged")
    manifest = unit_home / "skills" / "ranged" / "meiva-plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "ranged",
                "version": "1.0.0",
                "requires_commit": "abcdef1234",
            }
        ),
        encoding="utf-8",
    )
    assert (
        cmd_packages_status(
            config=process_config,
            now=datetime.now(UTC),
            home=unit_home,
            user_home=unit_home,
        )
        == 0
    )
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "commit abcdef1" in out


def test_host_blocked_package_surfaces_the_reason(
    process_config: ConfigBoot, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A manifest whose range excludes this host is a load-time drop, and the
    status surface carries the reason (design §5.5)."""
    _register("blocked", origin="repo", origin_path="/x/meiva_builtins/skills/blocked")
    manifest = unit_home / "skills" / "blocked" / "meiva-plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "blocked",
                "version": "1.0.0",
                "engines": {"meiva": ">=2099"},
            }
        ),
        encoding="utf-8",
    )

    assert (
        cmd_packages_status(
            config=process_config,
            json_output=True,
            now=datetime.now(UTC),
            home=unit_home,
            user_home=unit_home,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    (row,) = payload["packages"]
    assert row["host_blocked"] is not None and "2099" in row["host_blocked"]

    assert (
        cmd_packages_status(
            config=process_config,
            now=datetime.now(UTC),
            home=unit_home,
            user_home=unit_home,
        )
        == 0
    )
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "not loadable on this host" in out


# meiva:note
# A row recorded as `~/src/tilde-skill` (written before sources were recorded absolute) resolves
# like the absolute path it names: a local directory has no remote channel. The verdict follows the
# caller's home, not the process's.
def test_status_places_a_recorded_tilde_source_before_resolving_its_channel(
    process_config: ConfigBoot,
    unit_home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src" / "tilde-skill").mkdir(parents=True)
    _register("tilde-skill", origin="user", source="~/src/tilde-skill")

    def channel_here(user_home: Path) -> object:
        assert (
            cmd_packages_status(
                config=process_config,
                json_output=True,
                now=datetime.now(UTC),
                home=unit_home,
                user_home=user_home,
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        (row,) = payload["packages"]
        return row["channel"]

    assert channel_here(tmp_path / "elsewhere") == "git"
    assert channel_here(tmp_path) is None


# meiva:note
# `packages policy` reports the channel of the row it just wrote. A row recorded as
# `~/src/local-pkg` names a local directory, so it has none — the same verdict `packages status`
# gives for it.
def test_policy_reports_the_channel_a_placed_row_has(
    process_config: ConfigBoot, unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.extensions.packages import cmd_packages_policy

    (tmp_path / "src" / "local-pkg").mkdir(parents=True)
    _register("local-pkg", origin="user", source="~/src/local-pkg")

    assert (
        cmd_packages_policy(
            "local-pkg",
            config=process_config,
            update_mode="auto",
            home=unit_home,
            user_home=tmp_path,
        )
        == 0
    )
    assert "channel=-" in capsys.readouterr().out
