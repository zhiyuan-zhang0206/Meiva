# meiva:note
# The `~/.local/bin` PATH block converge keeps in the user's shell rc file.
#
# Pure text: the shell reads the rc file, asks `with_path_block` for its next content and
# writes it back when that differs.

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from base.host.env.agent_env import MACOS_GNU_BIN_DIRS

__all__ = [
    "MACOS_GNU_BIN_DIRS",
    "PATH_BEGIN",
    "PATH_END",
    "ZSHENV_BEGIN",
    "ZSHENV_END",
    "rc_file_name",
    "with_path_block",
    "with_zshenv_block",
]

PATH_BEGIN = "# >>> meiva path >>>"
PATH_END = "# <<< meiva path <<<"
ZSHENV_BEGIN = "# >>> meiva zshenv >>>"
ZSHENV_END = "# <<< meiva zshenv <<<"


# meiva:note
# The rc file name for a login shell path: zsh reads `.zshrc`, every other shell `.bashrc`.
def rc_file_name(shell: str) -> str:
    return ".zshrc" if shell.endswith("zsh") else ".bashrc"


# meiva:note
# `existing` with the PATH block present exactly once and the rest of the file untouched.
#
# A file that already carries the block gets it replaced in place; any other file gets it
# appended on a line of its own.
def with_path_block(
    existing: str, local_bin: Path, *, gnu_bin_dirs: Sequence[str] = MACOS_GNU_BIN_DIRS
) -> str:
    lines = [
        f'[ -d "{gnu_dir}" ] && case ":$PATH:" in *":{gnu_dir}:"*) ;; *) export PATH="{gnu_dir}:$PATH" ;; esac'
        for gnu_dir in gnu_bin_dirs
    ]
    lines.append(
        f'case ":$PATH:" in *":{local_bin}:"*) ;; *) export PATH="{local_bin}:$PATH" ;; esac'
    )
    body = "\n".join(lines)
    block = f"{PATH_BEGIN}\n{body}\n{PATH_END}"
    pattern = re.compile(re.escape(PATH_BEGIN) + r".*?" + re.escape(PATH_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(lambda _match: block, existing)
    separator = "" if existing == "" or existing.endswith("\n") else "\n"
    return f"{existing}{separator}{block}\n"


# meiva:note
# `existing` with the zshenv block present exactly once. Disables nomatch and bang_hist
# so raw `zsh -c` invocations never fail on unmatched patterns or history expansion.
def with_zshenv_block(existing: str) -> str:
    line = "unsetopt nomatch bang_hist 2>/dev/null || true"
    block = f"{ZSHENV_BEGIN}\n{line}\n{ZSHENV_END}"
    pattern = re.compile(re.escape(ZSHENV_BEGIN) + r".*?" + re.escape(ZSHENV_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(lambda _match: block, existing)
    separator = "" if existing == "" or existing.endswith("\n") else "\n"
    return f"{existing}{separator}{block}\n"
