"""`meiva packages refresh` — the content-channel executor contract (task #3267).

Unit locks for due/backoff math + the policy write-back, then integration
against a local git fixture: a real checkout with a bare `origin`, two skills,
one changed per commit — the design's §6 P1 acceptance checklist (single
machine).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from loguru import logger

from base.config import ConfigBoot
from base.packages.extensions import install_registry as reg
from base.paths.home import resolve_meiva_home
from cli.commands.duration import parse_duration
from cli.commands.extensions.packages.refresh import Pass, RefreshReport, run_refresh
from cli.commands.extensions.packages.refresh_git_plan import (
    core_fetch,
    core_listing,
    package_listing,
)
from cli.commands.extensions.packages.refresh_rules import (
    core_fetch_needed,
    effective_interval_seconds,
    is_due,
)


@pytest.fixture
def refresh_config() -> ConfigBoot:
    owner = ConfigBoot()
    owner.read_process_environment()
    return owner


@pytest.fixture(autouse=True)
def _scratch_home(unit_home: Path) -> None:
    (unit_home / "skills").mkdir()
    (unit_home / "logs").mkdir()


def _refresh(
    config: ConfigBoot,
    repo: Path,
    *,
    check_only: bool = False,
    only: str | None = None,
    from_job: bool = False,
) -> RefreshReport:
    return run_refresh(
        config=config,
        repo=repo,
        now=datetime.now(UTC),
        check_only=check_only,
        only=only,
        from_job=from_job,
        home=_home(),
    )


def _home() -> Path:
    return resolve_meiva_home()


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def _write_skill(
    repo: Path, name: str, body: str, manifest: dict[str, object] | None = None
) -> None:
    d = repo / "meiva_builtins" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8"
    )
    if manifest is not None:
        (d / "meiva-plugin.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _commit_push(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    _git(repo, "push", "-q", "origin", "main")
    return _head(repo)


@pytest.fixture
def core_repo(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "--initial-branch=main", str(bare)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _write_skill(repo, "foo", "# v1\n")
    _write_skill(repo, "bar", "# v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _seed(
    repo: Path,
    name: str,
    *,
    applied_rev: str | None,
    mode: reg.UpdateMode | None = None,
    last_check_at: str | None = None,
) -> None:
    src = repo / "meiva_builtins" / "skills" / name
    dest = _home() / "skills" / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*reg.IGNORED_NAMES))
    reg.register(
        reg.InstalledPackage(
            name=name,
            type="skill",
            origin="repo",
            origin_path=str(src),
            content_hash=reg.tree_hash(dest),
            update=reg.UpdateState(applied_rev=applied_rev, mode=mode, last_check_at=last_check_at),
        )
    )


def _mirror_installed_hash(name: str) -> None:
    """Give `name` the row shape `meiva skill install` writes: both edit guards set."""
    with reg.mutate() as registry:
        row = next(p for p in registry.packages if p.name == name)
        row.installed_hash = row.content_hash


def _row(name: str) -> reg.InstalledPackage:
    row = reg.get(name)
    assert row is not None
    return row


# ── due / backoff / duration math ───────────────────────────────────────────


def test_parse_duration() -> None:
    assert parse_duration("30s") == 30
    assert parse_duration("15m") == 900
    assert parse_duration("24h") == 86400
    assert parse_duration("7d") == 604800
    assert parse_duration("45") == 45
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_due_math_and_backoff() -> None:
    now = datetime.now(UTC)
    assert is_due(None, 3600, 0, name="x", now=now) is True
    recent = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    assert is_due(recent, 3600, 0, name="x", now=now) is False
    old = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    assert is_due(old, 3600, 0, name="x", now=now) is True
    # failures double the effective interval (within the +/-10% jitter band)
    eff = effective_interval_seconds(3600, 3, name="x", last_check_at=recent)
    assert 3600 * 8 * 0.9 <= eff <= 3600 * 8 * 1.1
    # many failures cap at a week
    capped = effective_interval_seconds(3600, 50, name="x", last_check_at=None)
    assert capped <= 7 * 24 * 3600
    # jitter is deterministic per (name, stamp)
    assert effective_interval_seconds(3600, 0, name="x", last_check_at=recent) == (
        effective_interval_seconds(3600, 0, name="x", last_check_at=recent)
    )


# ── integration: one changed package applies, idempotent ────────────────────


def _installed_skill_text(name: str) -> str:
    return (_home() / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def _assert_only_foo_content_changed_on_disk() -> None:
    assert "# v2" in _installed_skill_text("foo")
    assert "# v1" in _installed_skill_text("bar")
    assert (_home() / "skills" / ".foo.prev" / "SKILL.md").is_file()


def _assert_registry_advanced_to_core_head(c2: str) -> None:
    foo = _row("foo")
    assert foo.update.applied_rev == c2
    assert foo.update.mode == "auto" and foo.update.interval_seconds == 86400
    assert foo.update.channel == "core" and foo.update.failures == 0
    assert _row("bar").update.applied_rev == c2
    assert reg.load().channels["core"].last_seen_sha == c2


def test_refresh_applies_only_the_changed_package(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _seed(core_repo, "bar", applied_rev=c1)
    _write_skill(core_repo, "foo", "# v2\n")
    c2 = _commit_push(core_repo, "foo v2")

    report = _refresh(refresh_config, core_repo)
    assert report.ran
    by_name = {item.name: item for item in report.items}
    assert by_name["foo"].result == "applied"
    assert by_name["bar"].result == "up_to_date"
    assert report.channel_line is not None and c2[:7] in report.channel_line

    _assert_only_foo_content_changed_on_disk()
    # the checkout is never written
    assert _git(core_repo, "status", "--porcelain") == ""
    _assert_registry_advanced_to_core_head(c2)

    # idempotent second run: nothing to do, no content change
    again = _refresh(refresh_config, core_repo)
    assert {i.name: i.result for i in again.items} == {
        "foo": "up_to_date",
        "bar": "up_to_date",
    }
    assert _installed_skill_text("foo") == (
        (core_repo / "meiva_builtins" / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8")
    )


def test_refresh_apply_then_apply_with_installed_hash(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    """Rows carrying `installed_hash` (the `meiva skill install` shape) keep
    updating: apply / rollback write BOTH edit guards, so the second upstream
    change is not misread as a local edit. The guard prefers `installed_hash`
    (converge owns `content_hash` on installed-plugin rows), so a stale value
    would freeze every later apply and falsely refuse rollback (QA MF-1)."""
    from cli.commands.extensions.packages import cmd_packages_rollback

    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _mirror_installed_hash("foo")
    _write_skill(core_repo, "foo", "# v2\n")
    _commit_push(core_repo, "foo v2")

    assert _refresh(refresh_config, core_repo).items[0].result == "applied"
    row = _row("foo")
    assert row.installed_hash == row.content_hash  # the guards moved with the tree

    _write_skill(core_repo, "foo", "# v3\n")
    c3 = _commit_push(core_repo, "foo v3")
    second = _refresh(refresh_config, core_repo)
    assert second.items[0].result == "applied"  # no local-edit refusal
    assert _row("foo").update.applied_rev == c3

    # rollback reads the same baseline: no false refusal, and it dual-writes too
    assert cmd_packages_rollback("foo", home=_home(), now=datetime.now(UTC)) == 0
    row = _row("foo")
    assert row.installed_hash == row.content_hash
    assert "# v2" in (_home() / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8")


def test_refresh_reconciles_unknown_baseline_by_content(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    # Rows from before the channel carried no applied_rev.
    _seed(core_repo, "foo", applied_rev=None)
    _seed(core_repo, "bar", applied_rev=None)
    _write_skill(core_repo, "foo", "# v2\n")
    c2 = _commit_push(core_repo, "foo v2")

    report = _refresh(refresh_config, core_repo)
    by_name = {item.name: item for item in report.items}
    assert by_name["foo"].result == "applied"
    assert by_name["bar"].result == "up_to_date"  # staged content == disk
    assert _row("foo").update.applied_rev == c2
    assert _row("bar").update.applied_rev == c2
    home = _home()
    assert "# v2" in (home / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v1" in (home / "skills" / "bar" / "SKILL.md").read_text(encoding="utf-8")


def test_core_refresh_replaces_a_hand_edited_copy_and_reports_it(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _write_skill(core_repo, "foo", "# v2\n")
    c2 = _commit_push(core_repo, "foo v2")
    home = _home()
    copy = home / "skills" / "foo" / "SKILL.md"
    mine = "---\nname: foo\ndescription: MINE\n---\n\nhands off\n"
    copy.write_text(mine, encoding="utf-8")

    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="INFO", format="{message}")
    try:
        report = _refresh(refresh_config, core_repo)
    finally:
        logger.remove(sink)
    assert report.items[0].result == "applied"
    assert "# v2" in copy.read_text(encoding="utf-8")
    assert any("local copy replaced" in w and "SKILL.md" in w for w in warnings)
    assert (home / "skills" / ".foo.prev" / "SKILL.md").read_text(encoding="utf-8") == mine
    assert _row("foo").update.applied_rev == c2
    assert _row("foo").update.failures == 0


def test_git_channel_copy_edit_converges_to_incoming(
    refresh_config: ConfigBoot, core_repo: Path, tmp_path: Path
) -> None:
    """A tracked copy that no longer matches its baseline is converged, not
    blocked: the incoming tree lands and the replacement is reported (user
    ruling 2026-10-02/03 — local copies are never hand-edited)."""
    home = _home()
    dest = home / "skills" / "foo"
    dest.mkdir(parents=True)
    mine = "---\nname: foo\ndescription: MINE\n---\n\nedited\n"
    (dest / "SKILL.md").write_text(mine, encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "SKILL.md").write_text(
        "---\nname: foo\ndescription: d\n---\n\nfrom source\n", encoding="utf-8"
    )
    pkg = reg.InstalledPackage(
        name="foo",
        type="skill",
        origin="user",
        source="https://example.invalid/foo.git",
        content_hash="0" * 64,
        update=reg.UpdateState(channel="git"),
    )

    notes: list[str] = []
    sink = logger.add(lambda m: notes.append(str(m)), level="INFO", format="{message}")
    try:
        gate = Pass(
            config=refresh_config,
            check_only=False,
            only=None,
            from_job=False,
            now=datetime.now(UTC),
            repo=core_repo,
            home=_home(),
        )
        result, new_hash = gate._apply_staged(pkg, staged, "0" * 40)
    finally:
        logger.remove(sink)
    assert result == "applied" and new_hash is not None
    assert "from source" in (dest / "SKILL.md").read_text(encoding="utf-8")
    assert (home / "skills" / ".foo.prev" / "SKILL.md").read_text(encoding="utf-8") == mine
    assert any("local copy replaced" in n and "SKILL.md" in n for n in notes)


def test_refresh_skips_local_source_rows(
    refresh_config: ConfigBoot, core_repo: Path, tmp_path: Path
) -> None:
    """Local sources have no remote channel (design §5.1): the pass skips them
    with no error record and no backoff — including a stale persisted
    `channel: git` written before local sources were classified (QA MF-3)."""
    src = core_repo / "meiva_builtins" / "skills" / "foo"
    dest = _home() / "skills" / "foo"
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*reg.IGNORED_NAMES))

    def _register_local(source: str, *, channel: reg.ChannelKind | None = None) -> None:
        reg.register(
            reg.InstalledPackage(
                name="foo",
                type="skill",
                origin="user",
                source=source,
                enabled=True,
                content_hash=reg.tree_hash(dest),
                update=reg.UpdateState(channel=channel),
            )
        )

    for source in ("local:testbox", str(tmp_path)):
        _register_local(source)
        report = _refresh(refresh_config, core_repo)
        assert report.ran and "foo" not in {i.name for i in report.items}
        row = _row("foo")
        assert row.update.failures == 0
        assert row.update.last_result is None  # skipped, not recorded as an error

    _register_local("local:testbox", channel="git")
    assert _refresh(refresh_config, core_repo).ran
    assert _row("foo").update.failures == 0


def test_refresh_blocks_on_the_version_gate(refresh_config: ConfigBoot, core_repo: Path) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _write_skill(
        core_repo,
        "foo",
        "# v2\n",
        manifest={
            "apiVersion": 2,
            "name": "foo",
            "version": "2.0.0",
            "engines": {"meiva": ">=2099"},
        },
    )
    _commit_push(core_repo, "foo v2")

    report = _refresh(refresh_config, core_repo)
    assert report.items[0].result.startswith("blocked_version")
    home = _home()
    assert "# v1" in (home / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8")
    assert _row("foo").update.failures == 1
    # stale rev is kept, the block retries later
    assert _row("foo").update.applied_rev == c1


def test_refresh_never_deletes_a_package_removed_upstream(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    shutil.rmtree(core_repo / "meiva_builtins" / "skills" / "foo")
    _commit_push(core_repo, "drop foo")

    report = _refresh(refresh_config, core_repo)
    assert report.items[0].result.startswith("error")
    assert "converge" in report.items[0].result
    # old content stays; converge's cleanup owns removal
    assert (_home() / "skills" / "foo" / "SKILL.md").is_file()


def test_refresh_records_error_and_backs_off_when_offline(
    refresh_config: ConfigBoot, core_repo: Path, tmp_path: Path
) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _seed(core_repo, "bar", applied_rev=c1)
    _git(core_repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    report = _refresh(refresh_config, core_repo)
    assert {i.name: i.result.startswith("error") for i in report.items} == {
        "foo": True,
        "bar": True,
    }
    assert _row("foo").update.failures == 1
    assert (reg.load().channels["core"].last_result or "").startswith("error")

    # the job retries only after the backoff: same run parameters, not due now
    refresh_config.set_field("os_jobs_enabled", True)
    report = _refresh(refresh_config, core_repo, from_job=True)
    assert report.ran and report.items == ()
    assert report.counts.get("skipped_not_due") == 2


def test_check_only_records_available_without_applying(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _seed(core_repo, "bar", applied_rev=c1)
    _write_skill(core_repo, "foo", "# v2\n")
    _commit_push(core_repo, "foo v2")

    report = _refresh(refresh_config, core_repo, check_only=True)
    by_name = {item.name: item for item in report.items}
    assert by_name["foo"].result.startswith("available: ")
    assert by_name["bar"].result == "up_to_date"
    assert "# v1" in (_home() / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8")
    assert _row("foo").update.failures == 0


def test_notify_mode_records_available_without_applying(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1, mode="notify")
    _write_skill(core_repo, "foo", "# v2\n")
    _commit_push(core_repo, "foo v2")

    report = _refresh(refresh_config, core_repo)
    assert report.items[0].result.startswith("available: ")
    assert "# v1" in (_home() / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8")
    assert _row("foo").update.mode == "notify"  # explicit mode survives


# ── gates: flock / job switches / due cadence ───────────────────


def test_flock_skips_a_concurrent_pass(refresh_config: ConfigBoot, core_repo: Path) -> None:
    from base.native_process.os_platform import file_lock

    with file_lock(_home() / "packages-refresh.lock", timeout_s=1):
        report = _refresh(refresh_config, core_repo)
    assert not report.ran and "holds the lock" in (report.skip_reason or "")


def test_from_job_gates(refresh_config: ConfigBoot, core_repo: Path) -> None:
    # Suite default: OS jobs off -> the job run is a no-op.
    report = _refresh(refresh_config, core_repo, from_job=True)
    assert not report.ran and "OS jobs disabled" in (report.skip_reason or "")

    refresh_config.set_field("os_jobs_enabled", True)
    refresh_config.set_field("refresh_enabled", False)
    report = _refresh(refresh_config, core_repo, from_job=True)
    assert not report.ran and "refresh disabled" in (report.skip_reason or "")

    refresh_config.set_field("refresh_enabled", True)
    c1 = _head(core_repo)
    now_stamp = datetime.now(UTC).isoformat(timespec="seconds")
    _seed(core_repo, "foo", applied_rev=c1, last_check_at=now_stamp)
    _seed(core_repo, "bar", applied_rev=c1, last_check_at=now_stamp)
    report = _refresh(refresh_config, core_repo, from_job=True)
    assert report.ran and report.items == ()
    assert report.counts.get("skipped_not_due") == 2


def test_only_limits_the_pass(refresh_config: ConfigBoot, core_repo: Path) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _seed(core_repo, "bar", applied_rev=c1)
    _write_skill(core_repo, "foo", "# v2\n")
    _write_skill(core_repo, "bar", "# v2\n")
    _commit_push(core_repo, "both v2")

    report = _refresh(refresh_config, core_repo, only="foo")
    assert [item.name for item in report.items] == ["foo"]
    assert "# v1" in (_home() / "skills" / "bar" / "SKILL.md").read_text(encoding="utf-8")
    assert _row("bar").update.applied_rev == c1


def test_only_reports_a_skip_reason_for_untracked_names(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    report = _refresh(refresh_config, core_repo, only="nope")
    assert report.ran and report.items == ()
    assert any("no tracked channel-backed skill" in note for note in report.notes)


# ── rollback / policy verbs ─────────────────────────────────────────────────


def test_rollback_restores_the_previous_tree(
    refresh_config: ConfigBoot, core_repo: Path, capsys
) -> None:
    from cli.commands.extensions.packages import cmd_packages_rollback

    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _write_skill(core_repo, "foo", "# v2\n")
    _commit_push(core_repo, "foo v2")
    assert _refresh(refresh_config, core_repo).items[0].result == "applied"

    assert cmd_packages_rollback("foo", home=_home(), now=datetime.now(UTC)) == 0
    home = _home()
    assert "# v1" in (home / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8")
    assert "# v2" in (home / "skills" / ".foo.prev" / "SKILL.md").read_text(encoding="utf-8")
    assert (_row("foo").update.last_result or "").startswith("rolled_back")

    # a differing local copy is replaced (and reported), not refused
    copy = home / "skills" / "foo" / "SKILL.md"
    copy.write_text("---\nname: foo\ndescription: MINE\n---\n\nedited\n", encoding="utf-8")
    assert cmd_packages_rollback("foo", home=_home(), now=datetime.now(UTC)) == 0
    assert "current copy differs" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "# v2" in copy.read_text(encoding="utf-8")


def test_rollback_without_a_previous_tree_refuses(core_repo: Path) -> None:
    from cli.commands.extensions.packages import cmd_packages_rollback

    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    assert cmd_packages_rollback("foo", home=_home(), now=datetime.now(UTC)) == 1


def test_policy_verb_writes_explicit_values(refresh_config: ConfigBoot, core_repo: Path) -> None:
    from cli.commands.extensions.packages import cmd_packages_policy

    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    assert (
        cmd_packages_policy("foo", config=refresh_config, update_mode="notify", check_every="2h")
        == 0
    )
    row = _row("foo")
    assert row.update.mode == "notify" and row.update.interval_seconds == 7200
    assert cmd_packages_policy("foo", config=refresh_config, check_every="soon") == 1
    assert cmd_packages_policy("foo", config=refresh_config) == 1
    assert cmd_packages_policy("missing", config=refresh_config, update_mode="auto") == 1


# ── runtime host-contract filter (design §5.5) ──────────────────────────────


def test_refresh_cmd_json_shape(
    refresh_config: ConfigBoot, core_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.extensions.packages import cmd_packages_refresh

    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)

    assert (
        cmd_packages_refresh(
            config=refresh_config,
            repo=core_repo,
            now=datetime.now(UTC),
            json_output=True,
            home=_home(),
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    assert payload["ran"] is True
    (item,) = payload["items"]
    assert item["name"] == "foo" and item["mode"] == "auto"
    assert item["result"] == "up_to_date"
    assert "counts" in payload and "notes" in payload


# meiva:note
# Acceptance #6: consecutive runs are cheap. Once the head is applied and its objects are
# present, a pass asks `ls-remote` only; `git fetch` rewrites the checkout's `FETCH_HEAD`, so
# the file's modification time is the instrument, and the last step shows a real fetch moves it.
def test_second_run_checks_but_does_not_fetch(refresh_config: ConfigBoot, core_repo: Path) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _write_skill(core_repo, "foo", "# v2\n")
    _commit_push(core_repo, "foo v2")
    fetch_head = core_repo / ".git" / "FETCH_HEAD"

    assert _refresh(refresh_config, core_repo).items[0].result == "applied"
    fetched_at = fetch_head.stat().st_mtime_ns
    assert _refresh(refresh_config, core_repo).items[0].result == "up_to_date"
    assert fetch_head.stat().st_mtime_ns == fetched_at

    _write_skill(core_repo, "foo", "# v3\n")
    _commit_push(core_repo, "foo v3")
    assert _refresh(refresh_config, core_repo).items[0].result == "applied"
    assert fetch_head.stat().st_mtime_ns != fetched_at


# meiva:note
# The fetch decision, from values: the head moved since the channel was last seen, or some due
# package never applied, sits at another revision, or is failing.
@pytest.mark.parametrize(
    ("last_seen", "applied", "needed"),
    [
        ("a" * 40, [("a" * 40, 0), ("a" * 40, 0)], False),
        ("b" * 40, [("a" * 40, 0)], True),
        (None, [("a" * 40, 0)], True),
        ("a" * 40, [("a" * 40, 0), (None, 0)], True),
        ("a" * 40, [("a" * 40, 0), ("b" * 40, 0)], True),
        ("a" * 40, [("a" * 40, 2)], True),
        ("a" * 40, [], False),
    ],
)
def test_core_fetch_needed(
    last_seen: str | None, applied: list[tuple[str | None, int]], needed: bool
) -> None:
    assert core_fetch_needed("a" * 40, last_seen, applied) is needed


def test_refresh_preserves_marked_subtrees(refresh_config: ConfigBoot, core_repo: Path) -> None:
    """A marker-protected local subtree rides across the staged swap (the same
    contract converge's `_copy_tree` honors), and does not leak into `.prev`."""
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    adapter = _home() / "skills" / "foo" / "adapters"
    adapter.mkdir()
    (adapter / "local.py").write_text("# local adapter\n", encoding="utf-8")
    (adapter / ".preserved").write_text("", encoding="utf-8")
    _write_skill(core_repo, "foo", "# v2\n")
    _commit_push(core_repo, "foo v2")

    report = _refresh(refresh_config, core_repo)
    assert report.items[0].result == "applied"
    assert (adapter / "local.py").is_file()
    assert "# v2" in (_home() / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8")
    assert not (_home() / "skills" / ".foo.prev" / "adapters").exists()


# meiva:note
# The apply quota is read from the configuration owner the pass was handed: with room for one
# apply, two changed packages land one (least recently checked first, then by name) and the other
# is skipped on budget, its disk and its recorded revision untouched.
def test_the_apply_quota_comes_from_the_supplied_owner(
    refresh_config: ConfigBoot, core_repo: Path
) -> None:
    c1 = _head(core_repo)
    _seed(core_repo, "foo", applied_rev=c1)
    _seed(core_repo, "bar", applied_rev=c1)
    _write_skill(core_repo, "foo", "# v2\n")
    _write_skill(core_repo, "bar", "# v2\n")
    _commit_push(core_repo, "both v2")
    refresh_config.set_field("refresh_max_applies", 1)

    report = _refresh(refresh_config, core_repo)

    assert [(item.name, item.result) for item in report.items] == [("bar", "applied")]
    assert report.counts == {"applied": 1, "skipped_budget": 1}
    assert "# v2" in _installed_skill_text("bar")
    assert "# v1" in _installed_skill_text("foo")
    assert _row("foo").update.applied_rev == c1


# meiva:note
# Timeouts and command composition are values. The other cases in this module execute the real
# Git resources; no command spy is needed to prove the supplied 6/7 second bounds.
@pytest.mark.parametrize("timeout", [6.0, 7.0])
def test_network_command_plans_hold_the_supplied_timeout(tmp_path: Path, timeout: float) -> None:
    env = {"GIT_TERMINAL_PROMPT": "0"}
    commands = (
        core_listing(tmp_path, "https://example.invalid/core", "main", env=env, timeout=timeout),
        core_fetch(tmp_path, "https://example.invalid/core", "main", env=env, timeout=timeout),
        package_listing("https://example.invalid/skill", None, env=env, timeout=timeout),
    )
    assert [command.timeout for command in commands] == [timeout, timeout, timeout]
    assert [command.cwd for command in commands] == [tmp_path, tmp_path, None]
    assert [command.argv for command in commands] == [
        ("git", "ls-remote", "https://example.invalid/core", "main"),
        ("git", "fetch", "--no-tags", "https://example.invalid/core", "main"),
        ("git", "ls-remote", "https://example.invalid/skill", "HEAD"),
    ]
    assert all(command.env == env for command in commands)
    env["GIT_TERMINAL_PROMPT"] = "changed after planning"
    for command in commands:
        assert command.env == {"GIT_TERMINAL_PROMPT": "0"}
        # meiva:note
        # Even a caller that bypasses Mapping's static door cannot mutate the planned value.
        with pytest.raises(TypeError):
            cast("MutableMapping[str, str]", command.env)["GIT_TERMINAL_PROMPT"] = "changed"
    assert package_listing("remote", "branch", env=env, timeout=timeout).argv[-1] == "branch"


def test_refresh_rejects_unprepared_owner(core_repo: Path) -> None:
    with pytest.raises(ValueError, match="prepared configuration owner"):
        _refresh(ConfigBoot(), core_repo)
