# meiva:note
# `~` expanded against a home the caller read, never against the process's own.
#
# Every path a person types (a `--out` target, a package source, a skill directory) may start with
# `~`, and which home that names is a fact of the invocation: the entry that read it hands it here.
# A `~user` spelling names another account's home, which an invocation does not read, so it is
# refused rather than guessed. This is a function of values and is `strict`: `base.paths.home` owns
# the ambient home, this owns the leading `~` of a path a person typed, given that home as a value.

from __future__ import annotations

from pathlib import Path

__all__ = ["expand_home"]


# meiva:note
# `path` with a leading `~` expanded against `user_home`: `~` and `~/...` become paths under that
# home (a doubled slash after the tilde does not make the rest absolute), an absolute path is kept
# as it is, and a relative one is left relative for the caller to place. A `~someone/...` spelling
# raises `ValueError`: it names another account's home, which an invocation does not read.
def expand_home(path: str | Path, *, user_home: Path) -> Path:
    raw = path if isinstance(path, str) else str(path)
    if raw == "~":
        return user_home
    if raw.startswith("~/"):
        return user_home / raw[2:].lstrip("/")
    if raw.startswith("~"):
        raise ValueError(
            f"path {raw!r} names another user's home; "
            "give an absolute path, or one starting with ~/ or ~"
        )
    return Path(raw)
