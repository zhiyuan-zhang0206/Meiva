# meiva:note
# Warnings converge raises about the state of the checkout it runs from.
#
# Pure over what the shell read: the argv of the Git hook scan and the warning lines its output
# carries, and the notice for migration files git does not track.

from __future__ import annotations

from pathlib import Path

__all__ = ["hook_scan_argv", "hook_scan_warnings", "untracked_migrations_notice"]


# meiva:note
# The stdlib-only hook checker, run from THIS checkout over the whole machine.
def hook_scan_argv(python: str, script: Path) -> tuple[str, ...]:
    return (python, str(script), "--scan-machine")


# meiva:note
# The operator-visible lines of one hook scan.
#
# Exit 0 is a clean machine and 1 is a scan that found problems; anything else (a checkout
# that predates the flag answers a usage error) is silence, because a warn-only assertion
# must never make converge noisy or fatal on its own account.
def hook_scan_warnings(returncode: int, stdout: str) -> tuple[str, ...]:
    if returncode not in (0, 1):
        return ()
    return tuple(
        f"  ! hooks: {line.removeprefix('WARNING: ').strip()}"
        for line in stdout.splitlines()
        if line.startswith("WARNING:")
    )


# meiva:note
# The console warning for migration files git does not track, None when there are none.
#
# The applier skips untracked migrations with a log warning; this surfaces the same fact on
# the console so a migration written into the prod checkout without a commit is not read as
# having run.
def untracked_migrations_notice(names: tuple[str, ...]) -> str | None:
    if not names:
        return None
    return (
        f"⚠ [migration] {len(names)} untracked file(s) in migrations/ are NOT "
        f"in git and will NOT be applied: {', '.join(names)}"
    )
