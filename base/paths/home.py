# meiva:note
# The home directory a process acts on, and which checkout may operate it.
#
# `home_from_env` is pure over the values it is given; `resolve_meiva_home` and
# `default_home` are the ambient readers of `os.environ` and `Path.home()` that a
# composition root calls once. Everything that needs `$MEIVA_HOME`
# (the `.env` boot, the path helpers, the CLI's pre-Settings gate) can import this
# module without Settings, a log sink or a database.


from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

__all__ = [
    "default_home",
    "home_checkout_error",
    "home_from_env",
    "is_default_home",
    "is_prod_install",
    "resolve_meiva_home",
]


# meiva:note
# The home `env` names: `MEIVA_HOME` when set and non-empty, else `<user_home>/.meiva`.
#
# Pure: the user's home directory is a value too, so a leading `~` in `MEIVA_HOME`
# expands against `user_home`, never against the process's own `HOME`. A `~user`
# spelling is not a home and is refused like any other non-absolute path.
#
# Raises:
#     ValueError: `MEIVA_HOME` is not an absolute path; a relative home would
#         follow the working directory.
def home_from_env(env: Mapping[str, str], *, user_home: Path) -> Path:
    raw = env.get("MEIVA_HOME")
    if not raw:
        return user_home / ".meiva"
    if raw == "~":
        home = user_home
    elif raw.startswith("~/"):
        home = user_home / raw[2:]
    else:
        home = Path(raw)
    if not home.is_absolute():
        raise ValueError(f"MEIVA_HOME must be an absolute path, got {raw!r}")
    return home


# meiva:note
# This process's home: `$MEIVA_HOME` when set, else `~/.meiva`.
#
# The ambient reader for composition roots (process entry points and CLI command
# entries): it reads `os.environ` and `Path.home()` on every call, never cached,
# so a caller that changes the variable (a test session, a tool that sets it once
# at its top) is followed by every later call. Code below a root takes the home
# (or `home_from_env`'s inputs) as a value. The path is neither created nor checked
# here (`base.paths.meiva_home` creates it).
#
# Raises:
#     ValueError: `$MEIVA_HOME` is not an absolute path.
def resolve_meiva_home() -> Path:
    return home_from_env(os.environ, user_home=Path.home())


# meiva:note
# Why `repo` may not operate `home`, or None when it may.
#
# A home that carries its own `<home>/source` checkout (the production home
# `~/.meiva`, and every unit started from source) is operated only by that
# checkout's code: a disposable checkout driving it would bind its daemons to
# code that disappears with the checkout (the 01:13 worktree accident, Task
# #966) or apply un-reviewed migrations to its database. A home with no
# `source` (a test session's temporary home, a scratch `MEIVA_HOME`) accepts any
# checkout.
#
# Both sides are compared resolved, so a symlinked or `..`-spelled path to
# the same directory is the same checkout.
def home_checkout_error(repo: Path, *, home: Path) -> str | None:
    source = home / "source"
    if not source.is_dir() or repo.resolve() == source.resolve():
        return None
    return (
        f"this checkout ({repo}) is not the checkout of the home it would act on: "
        f"{home} carries its own source checkout ({source}), and only that "
        f"checkout may operate it. Run {source}/.venv/bin/meiva (the bare `meiva` of a "
        f"production host) instead, or set MEIVA_HOME to a home of your own (a "
        f"temporary directory, for a test or a tool)."
    )


# meiva:note
# The default (prod) cluster home, `~/.meiva` — the one home whose initialization
# uses the fixed default port block and whose checkout anchors without a
# pointer. The ambient reader for composition roots.
def default_home() -> Path:
    return Path.home() / ".meiva"


def is_default_home(home: Path) -> bool:
    """Whether `home` is the default prod home (`~/.meiva`)."""
    return Path(home).expanduser().resolve() == default_home().resolve()


# meiva:note
# Host-global wiring (the `~/.local/bin/meiva` symlink, the shell rc edit, the boot
# unit) belongs to the host's one prod install, never to a dev cluster spun up from
# a worktree: a worktree's wiring would dangle once the checkout is removed. The
# home must be the default one too, because a dev worktree resolves its home to
# `~/.meiva` whenever MEIVA_HOME is unset.
def is_prod_install(
    *, repo: Path, home: Path, is_default: Callable[[Path], bool] = is_default_home
) -> bool:
    resolved = str(repo.resolve())
    if ".claude/worktrees" in resolved or "/.worktrees/" in resolved:
        return False
    return is_default(home)
