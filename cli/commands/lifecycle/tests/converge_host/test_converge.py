# meiva:note
# The host-wiring converge steps, the shell rc block and the whole step table on a fresh unit.

from __future__ import annotations

import os
import socket
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from base.cluster.dataplane import runtime_binaries as rb
from base.host.system.tests.fake_commands import fake_host
from base.host.tests.short_tmp import short_tmp as short_tmp
from cli.commands.converge import frontend_env as _fe_env
from cli.commands.converge import steps as _steps
from cli.commands.converge.plans.shell_rc import PATH_BEGIN, PATH_END, rc_file_name, with_path_block
from cli.commands.converge.redis_bridge import retire_launchd_redis_bridge
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host

# meiva:note
# --- the shell rc block (pure) ------------------------------------------------------


@pytest.mark.parametrize(
    ("shell", "name"),
    [
        ("/bin/zsh", ".zshrc"),
        ("/opt/homebrew/bin/zsh", ".zshrc"),
        ("/bin/bash", ".bashrc"),
        ("/usr/bin/fish", ".bashrc"),
        ("", ".bashrc"),
    ],
)
def test_zsh_users_get_zshrc_and_every_other_shell_bashrc(shell: str, name: str) -> None:
    assert rc_file_name(shell) == name


def test_the_path_block_is_appended_to_a_file_that_lacks_it(tmp_path: Path) -> None:
    local_bin = tmp_path / ".local" / "bin"

    text = with_path_block("export FOO=1\n", local_bin)

    assert text.startswith("export FOO=1\n" + PATH_BEGIN + "\n")
    assert text.endswith(PATH_END + "\n")
    assert f'export PATH="{local_bin}:$PATH"' in text


def test_the_path_block_starts_a_new_line_after_an_unterminated_last_line(
    tmp_path: Path,
) -> None:
    text = with_path_block("export FOO=1", tmp_path)

    assert text.startswith("export FOO=1\n" + PATH_BEGIN)


def test_an_empty_file_holds_only_the_path_block(tmp_path: Path) -> None:
    text = with_path_block("", tmp_path)

    assert text.startswith(PATH_BEGIN) and text.count(PATH_BEGIN) == 1


def test_an_existing_block_is_replaced_in_place_and_the_rest_is_kept(tmp_path: Path) -> None:
    old = with_path_block("before\n", tmp_path / "old-bin") + "after\n"

    text = with_path_block(old, tmp_path / "new-bin")

    assert text.count(PATH_BEGIN) == 1
    assert "old-bin" not in text and str(tmp_path / "new-bin") in text
    assert text.startswith("before\n") and text.endswith("after\n")


def test_applying_the_path_block_twice_changes_nothing(tmp_path: Path) -> None:
    once = with_path_block("export FOO=1\n", tmp_path)

    assert with_path_block(once, tmp_path) == once


# meiva:note
# --- the host-wiring steps against a real user home ---------------------------------


def test_ensure_meiva_on_path_links_bare_meiva_to_this_checkouts_cli(
    ctx: ConvergeCtx, user_home: Path
) -> None:
    link = user_home / ".local" / "bin" / "meiva"
    link.parent.mkdir(parents=True)
    link.symlink_to(ctx.repo / "scripts" / "meiva-launcher.sh")  # the retired launcher link

    _steps.ensure_meiva_on_path(ctx)
    assert link.readlink() == ctx.repo / ".venv" / "bin" / "meiva"
    _steps.ensure_meiva_on_path(ctx)  # idempotent
    assert link.readlink() == ctx.repo / ".venv" / "bin" / "meiva"


def test_ensure_local_bin_on_path_writes_the_block_once_into_the_users_rc(
    ctx: ConvergeCtx, user_home: Path
) -> None:
    user_home.mkdir()
    rc = user_home / ".zshrc"
    rc.write_text("export FOO=1\n")

    _steps.ensure_local_bin_on_path(ctx)
    _steps.ensure_local_bin_on_path(ctx)

    text = rc.read_text()
    assert text.count(PATH_BEGIN) == 1
    assert "export FOO=1" in text
    assert str(user_home / ".local" / "bin") in text


def test_ensure_local_bin_on_path_creates_the_rc_of_the_users_shell(
    ctx: ConvergeCtx, user_home: Path
) -> None:
    user_home.mkdir()
    bash = replace(ctx, session=replace(ctx.session, login_shell="/bin/bash"))

    _steps.ensure_local_bin_on_path(bash)

    assert PATH_BEGIN in (user_home / ".bashrc").read_text()
    assert not (user_home / ".zshrc").exists()


def test_ensure_meiva_home_dirs(ctx: ConvergeCtx) -> None:
    _steps.ensure_meiva_home_dirs(ctx)

    for sub in ("logs", "configs", "secrets"):
        assert (ctx.meiva_home / sub).is_dir()
    # meiva:note
    # Exclude the whole runtime tree from Spotlight from the home root down: one marker
    # there covers logs, workspaces, checkouts and memory (mds_stores would otherwise
    # consume GBs of memory). Exactly one marker — a second one under `logs/` would state
    # nothing new and only leave a second fact to keep true.
    assert (ctx.meiva_home / ".metadata_never_index").is_file()
    assert not (ctx.meiva_home / "logs" / ".metadata_never_index").exists()


def test_ensure_meiva_home_dirs_recursively_converges_private_data_trees(
    ctx: ConvergeCtx,
) -> None:
    targets = (
        ctx.meiva_home / "logs" / "daemon" / "current.log",
        ctx.meiva_home / "workspaces" / "7" / "download.txt",
        ctx.meiva_home / "memory" / ".git" / "config",
    )
    for target in targets:
        target.parent.mkdir(parents=True)
        target.write_text("private")
        target.chmod(0o644)
        for parent in (target.parent, target.parent.parent):
            parent.chmod(0o755)

    _steps.ensure_meiva_home_dirs(ctx)

    for target in targets:
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.parent.parent.stat().st_mode) == 0o700


# meiva:note
# A leftover socket must not abort convergence (2026-09-12 outage). macOS needs
# a short root for AF_UNIX; pytest paths exceed its roughly 104-byte limit.
def test_ensure_meiva_home_dirs_survives_a_workspace_socket(
    ctx: ConvergeCtx, short_tmp: Path
) -> None:
    meiva_home = short_tmp / "meivahome"
    socket_dir = meiva_home / "workspaces" / "6063" / "f13b-poc"
    socket_dir.mkdir(parents=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        socket_path = socket_dir / "app.sock"
        server.bind(str(socket_path))

        _steps.ensure_meiva_home_dirs(replace(ctx, meiva_home=meiva_home))

        assert stat.S_ISSOCK(socket_path.lstat().st_mode)  # left in place
        assert stat.S_IMODE((meiva_home / "workspaces").stat().st_mode) == 0o700
        assert (meiva_home / ".metadata_never_index").exists()
    finally:
        server.close()


def test_ensure_meiva_home_dirs_rejects_logs_symlink_before_writing_marker(
    ctx: ConvergeCtx, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx.meiva_home.mkdir()
    (ctx.meiva_home / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match=r"logs.*symlink"):
        _steps.ensure_meiva_home_dirs(ctx)

    assert not (outside / ".metadata_never_index").exists()


def test_host_wiring_leaves_existing_editable_install_unchanged(
    ctx: ConvergeCtx, tmp_path: Path, user_home: Path
) -> None:
    """Host setup has no automatic source repair or package reinstall authority."""
    user_home.mkdir()
    source = tmp_path / "source"
    site = source / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    pointer = site / "_editable_impl_meiva.pth"
    pointer.write_text(str(tmp_path / "deleted-checkout"))
    scripts = source / ".venv" / "bin"
    scripts.mkdir()
    files = {pointer: pointer.read_bytes()}
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in (site, scripts, pointer)}
    steps = tuple(
        step
        for step in converge_host.CONVERGE_STEPS
        if step.host_global and step.apply.__module__ == _steps.__name__
    )

    converge_host.run_converge(replace(ctx, repo=source, roles=None), steps, prod_install=True)

    assert {path: path.read_bytes() for path in files} == files
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in modes} == modes
    assert not (scripts / "meiva").exists()
    assert (user_home / ".local" / "bin" / "meiva").is_symlink()


# meiva:note
# --- the whole step table on a fresh unit -------------------------------------------


# meiva:note
# The prod install of a unit that is not configured yet runs the step table clean.
#
# The unit-state steps are deferred, so the permissions helper (`ctx.helper` refuses to
# answer) is never reached, and the host-global wiring lands in the user's home. The standalone
# Redis-bridge retirement is left out: it drives the real `launchctl` of the machine running the
# suite (it reads `sys.platform`, not `ctx.host`), which no test of this table may do.
def test_a_fresh_unit_converges_every_step_that_needs_no_unit_configuration(
    ctx: ConvergeCtx, default_home: Path
) -> None:
    meiva_home = default_home / ".meiva"
    repo = meiva_home / "source"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / "migrations").mkdir()
    # meiva:note
    # Seed installed Postgres binaries; their download has a dedicated contract test.
    seeded_bin = rb.vendored_pg_dir() / "bin"
    seeded_bin.mkdir(parents=True)
    (seeded_bin / "initdb").write_text("#!/bin/sh\n")
    # meiva:note
    # Seed pgvector too; its injection is covered by the runtime smoke test.
    seeded_ext = rb.vendored_pg_dir() / "share/postgresql/extension"
    seeded_ext.mkdir(parents=True)
    (seeded_ext / rb.PGVECTOR_SQL).write_text("-- seeded\n")
    steps = tuple(
        step
        for step in converge_host.CONVERGE_STEPS
        if step.apply is not retire_launchd_redis_bridge
    )
    unconfigured = replace(
        ctx,
        repo=repo,
        meiva_home=meiva_home,
        roles=None,
        host=fake_host(uid=os.getuid()),
    )

    converge_host.run_converge(unconfigured, steps, prod_install=True)

    assert (default_home / ".local" / "bin" / "meiva").is_symlink()
    assert (meiva_home / "logs").is_dir()


# meiva:note
# --- the frontend build-time env guard ----------------------------------------------


def test_frontend_env_override_guard_passes_clean(ctx: ConvergeCtx) -> None:
    (ctx.repo / "ui" / "web").mkdir(parents=True)
    (ctx.repo / "ui" / "web" / ".env.development").write_text("# tracked, next-dev-only\n")
    ctx.meiva_home.mkdir()
    # A unit .env carrying only MEIVA_* vars (the legitimate case) must pass.
    (ctx.meiva_home / ".env").write_text("MEIVA_GATEWAY_PORT=8800\nMEIVA_CLUSTER=main\n")

    _fe_env.ensure_no_frontend_env_overrides(ctx)  # must not raise


@pytest.mark.parametrize("name", _fe_env.FORBIDDEN_FRONTEND_ENV_FILES)
def test_frontend_env_override_guard_rejects_build_time_files(ctx: ConvergeCtx, name: str) -> None:
    """`next build` bakes NEXT_PUBLIC_* from these files into the bundle,
    silently beating the runtime gateway inference (2026-06-09 prod outage)."""
    (ctx.repo / "ui" / "web").mkdir(parents=True)
    (ctx.repo / "ui" / "web" / name).write_text("NEXT_PUBLIC_API_BASE=https://dead.example\n")

    with pytest.raises(RuntimeError, match="build-time env override"):
        _fe_env.ensure_no_frontend_env_overrides(ctx)


def test_frontend_env_override_guard_rejects_next_public_in_unit_env(ctx: ConvergeCtx) -> None:
    """The 2026-06-23 outage baked a stale unit-env port into the bundle.
    load_meiva_env exports every key; NEXT_PUBLIC_* belongs only on the build command."""
    (ctx.repo / "ui" / "web").mkdir(parents=True)
    ctx.meiva_home.mkdir()
    (ctx.meiva_home / ".env").write_text("MEIVA_GATEWAY_PORT=8000\nNEXT_PUBLIC_GATEWAY_PORT=8800\n")

    with pytest.raises(RuntimeError, match="NEXT_PUBLIC_GATEWAY_PORT"):
        _fe_env.ensure_no_frontend_env_overrides(ctx)


@pytest.mark.parametrize(
    "line",
    [
        "NEXT_PUBLIC_GATEWAY_PORT=8800",
        "  NEXT_PUBLIC_GATEWAY_PORT=8800",  # leading whitespace
        "export NEXT_PUBLIC_API_BASE=https://x",  # `export ` prefix
    ],
)
def test_next_public_keys_detects_assignment_shapes(tmp_path: Path, line: str) -> None:
    env = tmp_path / ".env"
    env.write_text(f"MEIVA_CLUSTER=main\n{line}\n")
    assert _fe_env.next_public_keys_in_env_file(env)


def test_next_public_keys_ignores_comments_and_substrings(tmp_path: Path) -> None:
    """A commented-out line or a var that merely contains the substring must not trip."""
    env = tmp_path / ".env"
    env.write_text(
        "# NEXT_PUBLIC_GATEWAY_PORT=8800\n"  # comment, not an assignment
        "MY_NEXT_PUBLIC_THING=1\n"  # substring, not a NEXT_PUBLIC_* key
        "MEIVA_GATEWAY_PORT=8000\n"
    )
    assert _fe_env.next_public_keys_in_env_file(env) == []


def test_next_public_keys_absent_file_is_empty(tmp_path: Path) -> None:
    assert _fe_env.next_public_keys_in_env_file(tmp_path / "nope.env") == []
