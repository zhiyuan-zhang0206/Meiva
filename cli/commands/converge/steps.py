"""Early host-wiring and unit-state converge steps."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from base.host.private_storage import converge_private_tree, ensure_private_file
from cli.commands.converge.plans.checkout_warnings import hook_scan_argv, hook_scan_warnings
from cli.commands.converge.plans.shell_rc import rc_file_name, with_path_block
from cli.commands.converge.spec import ConvergeCtx

# --- host-wiring steps (no preconditions) ---------------------------------


def _ensure_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.readlink() == target:
        return
    link.unlink(missing_ok=True)
    link.symlink_to(target)


# meiva:note
# The host's bare `meiva` -> this checkout's `.venv/bin/meiva`.
#
# A plain symlink: the host runs one cluster, so the CLI needs no routing. Which home it acts on is
# `MEIVA_HOME`, else `~/.meiva`, like every CLI, and the checkout guard
# (`cli.preflight.check_own_checkout`) refuses every command from a checkout that is not the home's
# own. An existing link to anything else is repointed.
def ensure_meiva_on_path(ctx: ConvergeCtx) -> None:
    _ensure_symlink(
        ctx.session.user_home / ".local" / "bin" / "meiva", ctx.repo / ".venv" / "bin" / "meiva"
    )


def ensure_local_bin_on_path(ctx: ConvergeCtx) -> None:
    rc = ctx.session.user_home / rc_file_name(ctx.session.login_shell)
    existing = rc.read_text() if rc.exists() else ""
    updated = with_path_block(existing, ctx.session.user_home / ".local" / "bin")
    if updated != existing:
        rc.write_text(updated)


def ensure_meiva_home_dirs(ctx: ConvergeCtx) -> None:
    for sub in ("configs", "secrets"):
        (ctx.meiva_home / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("logs", "workspaces", "memory"):
        converge_private_tree(ctx.meiva_home / sub)
    # meiva:note
    # The Spotlight opt-out marker sits at the HOME ROOT, so one file excludes the whole
    # runtime tree: source checkouts, worktrees, workspaces, the memory repo. A marker
    # under `logs/` excluded only the rotating logs while mds_stores and fseventsd kept
    # churning metadata over everything else (2026-10-24: fseventsd RSS 1.1 -> 2.1 GB
    # across the same window as two disk steps). Converge owns it because a hand-made
    # file is dropped by the next cleanup or migration.
    marker = ctx.meiva_home / ".metadata_never_index"
    marker.touch(exist_ok=True)
    ensure_private_file(marker)


def ensure_pg_binaries_step(
    ctx: ConvergeCtx,
    *,
    system: str | None = None,
    machine: str | None = None,
    platform_bin_dir: Path | None = None,
    path_env: str | None = None,
) -> None:
    """Prepare the selected PostgreSQL 17 runtime and its pgvector extension."""
    if ctx.read_config().view.data_plane.is_remote:
        return
    from base.cluster.dataplane.pg_runtime import ensure_pg_runtime

    ensure_pg_runtime(
        home=ctx.meiva_home,
        system=system,
        machine=machine,
        platform_bin_dir=platform_bin_dir,
        path_env=path_env,
    )


# meiva:note
# The hook checker lives in the checkout converge runs from.
_GIT_HOOKS_SCAN = Path("base") / "deploy" / "git" / "check_git_hooks.py"
_GIT_HOOKS_SCAN_TIMEOUT_S = 30.0


# meiva:note
# Warn when a conventional local checkout's Git hook installation is
# missing or drifted; never repair or block start.
#
# Runs the stdlib-only checker (``base/deploy/git/check_git_hooks.py
# --scan-machine``) from THIS checkout and mirrors its ``WARNING`` lines to
# stderr. A checkout that predates the flag (usage error), a missing script,
# or any subprocess failure degrades to silence — a warn-only assertion must
# never make converge noisy or fatal on its own account.
def ensure_local_git_hooks(ctx: ConvergeCtx) -> None:
    script = ctx.repo / _GIT_HOOKS_SCAN
    if not script.exists():
        return
    try:
        result = ctx.host.run(
            list(hook_scan_argv(sys.executable, script)),
            capture_output=True,
            text=True,
            timeout=_GIT_HOOKS_SCAN_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for line in hook_scan_warnings(result.returncode, result.stdout):
        print(line, file=sys.stderr)


# --- unit-state steps (need a configured unit) ----------------------------
