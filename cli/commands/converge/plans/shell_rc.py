# meiva:note
# The `~/.local/bin` PATH block converge keeps in the user's shell rc file.
#
# Pure text: the shell reads the rc file, asks `with_path_block` for its next content and
# writes it back when that differs.

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["PATH_BEGIN", "PATH_END", "rc_file_name", "with_path_block"]

PATH_BEGIN = "# >>> meiva path >>>"
PATH_END = "# <<< meiva path <<<"


# meiva:note
# The rc file name for a login shell path: zsh reads `.zshrc`, every other shell `.bashrc`.
def rc_file_name(shell: str) -> str:
    return ".zshrc" if shell.endswith("zsh") else ".bashrc"


# meiva:note
# `existing` with the PATH block present exactly once and the rest of the file untouched.
#
# A file that already carries the block gets it replaced in place; any other file gets it
# appended on a line of its own.
def with_path_block(existing: str, local_bin: Path) -> str:
    line = f'case ":$PATH:" in *":{local_bin}:"*) ;; *) export PATH="{local_bin}:$PATH" ;; esac'
    block = f"{PATH_BEGIN}\n{line}\n{PATH_END}"
    pattern = re.compile(re.escape(PATH_BEGIN) + r".*?" + re.escape(PATH_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(lambda _match: block, existing)
    separator = "" if existing == "" or existing.endswith("\n") else "\n"
    return f"{existing}{separator}{block}\n"
