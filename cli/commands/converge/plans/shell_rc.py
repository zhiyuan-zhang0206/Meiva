# meiva:note
# The PATH blocks converge keeps in the user's shell rc files: the Homebrew prefixes and
# `~/.local/bin` for a login shell, and the same for `~/.zshenv`, which is all a non-login
# shell reads.
#
# Pure text: the shell reads the rc file, asks `with_path_block` for its next content and
# writes it back when that differs.

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from base.host.env.agent_env import HOMEBREW_PREFIXES, MACOS_GNU_BIN_DIRS

__all__ = [
    "HOMEBREW_PREFIXES",
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
# The Homebrew lines, one per prefix the host has, appended rather than prepended: the block
# exists to make a formula reachable, never to change which interpreter a command resolves.
# That resolution belongs to the runtime (the venv, the system, a harness toolchain), not to a
# shell rc file. Prepending was measured on 2026-11-07: `ssh company-mini python3 -V` answered
# 3.14.7 (Homebrew) where it had answered the system 3.9.6, and `pip3`, `node` and `openssl`
# moved with it (#6433). Before making a line here prepend again, answer why this file should
# own that choice.
def _homebrew_path_lines() -> list[str]:
    return [
        f'[ -d "{prefix}/bin" ] && case ":$PATH:" in *":{prefix}/bin:"*) ;; *)'
        f' export PATH="$PATH:{prefix}/bin:{prefix}/sbin" ;; esac'
        for prefix in HOMEBREW_PREFIXES
    ]


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
    lines.extend(_homebrew_path_lines())
    body = "\n".join(lines)
    block = f"{PATH_BEGIN}\n{body}\n{PATH_END}"
    pattern = re.compile(re.escape(PATH_BEGIN) + r".*?" + re.escape(PATH_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(lambda _match: block, existing)
    separator = "" if existing == "" or existing.endswith("\n") else "\n"
    return f"{existing}{separator}{block}\n"


# meiva:note
# `existing` with the zshenv block present exactly once. Disables nomatch and bang_hist and
# puts the Homebrew prefixes, the GNU tools and ~/.local/bin on PATH, so a non-interactive
# `zsh -c` invocation — what `ssh host '<command>'` runs — never fails on an unmatched pattern
# or on a binary the host does have.
def with_zshenv_block(
    existing: str,
    local_bin: Path | None = None,
    *,
    gnu_bin_dirs: Sequence[str] = MACOS_GNU_BIN_DIRS,
) -> str:
    lines = ["unsetopt nomatch bang_hist 2>/dev/null || true"]
    for gnu_dir in gnu_bin_dirs:
        lines.append(
            f'[ -d "{gnu_dir}" ] && case ":$PATH:" in *":{gnu_dir}:"*) ;; *) export PATH="{gnu_dir}:$PATH" ;; esac'
        )
    if local_bin is not None:
        lines.append(
            f'case ":$PATH:" in *":{local_bin}:"*) ;; *) export PATH="{local_bin}:$PATH" ;; esac'
        )
    lines.extend(_homebrew_path_lines())
    body = "\n".join(lines)
    block = f"{ZSHENV_BEGIN}\n{body}\n{ZSHENV_END}"
    pattern = re.compile(re.escape(ZSHENV_BEGIN) + r".*?" + re.escape(ZSHENV_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(lambda _match: block, existing)
    separator = "" if existing == "" or existing.endswith("\n") else "\n"
    return f"{existing}{separator}{block}\n"
