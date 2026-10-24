# meiva:note
# The shell text each host runs, as values. A window sends these over SSH unchanged: the hosts'
# own checkout, venv and `meiva` do the work, and this module names the order of their words.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import shlex

HOME = 'H="$HOME/.meiva"; S="$H/source"; cd "$S" || exit 1'
MEIVA = 'MEIVA_HOME="$H" "$S/.venv/bin/meiva"'
PYTHON = f'{HOME}; MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python"'

# meiva:note
# The interpreter a program the window ships reads its source from: the host's own venv python, a
# program on stdin, so the first update that carries a program already has it.
PROGRAM = f"{PYTHON} -"

CLEAN = (
    'd=$(git status --porcelain | head -n 20); [ -z "$d" ] || { echo "$d"; echo "Untracked files '
    "are not caused by this update (a changed .gitignore can reveal them): check them, then "
    'move them away (do not delete) and rerun down."; exit 1; }'
)

OP_EVIDENCE = """H="$HOME/.meiva"
p=$(cat "$H/run/agent_host.pid" 2>/dev/null)
if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then echo host=up; else echo host=down; fi
cat "$H/run/lifecycle-op.json" 2>/dev/null || true"""

INCOMPLETE = 'cat "$HOME/.meiva/state/converge-incomplete.json" 2>/dev/null'

# meiva:note
# What a host reports before a window touches it: its checkout, whether the tree is clean, a
# post-checkout hook, an unfinished release operation, its last lifecycle journal and whether a
# meiva-root still holds its instance lock. One `key=value` per line.
PROBE = f"""{HOME}
printf '%s\\n' "head=$(git rev-parse HEAD)"
printf '%s\\n' "dirty=$(git status --porcelain 2>&1 | wc -l | tr -d ' ')"
test -e "$(git rev-parse --git-path hooks/post-checkout)" && echo hook=yes || echo hook=no
test -e "$H/updates/active" && echo active=yes || echo active=no
printf '%s\\n' "op=$(cat "$H/run/lifecycle-op.json" 2>/dev/null)"
"$S/.venv/bin/python" -c 'import fcntl, sys
try:
    fcntl.flock(open(sys.argv[1], "rb"), fcntl.LOCK_SH | fcntl.LOCK_NB)
    print("root=down")
except FileNotFoundError:
    print("root=down")
except OSError:
    print("root=up")' "$H/run/meiva-root/meiva-root.lock" 2>/dev/null"""

_VENV_DIRS = (
    "find .venv/bin .venv/lib -maxdepth 3 -type d \\( -path .venv/bin "
    '-o -path ".venv/lib/python*/site-packages" '
    '-o -path ".venv/lib/python*/site-packages/meiva-*.dist-info" \\) -exec chmod u+w {} +'
)


# meiva:note
# Make NEW's commit present on the host, so a host that cannot get it is found before anything
# stops.
def fetch_command(new: str) -> str:
    return f'{HOME}; git cat-file -e "{new}^{{commit}}" || git fetch -q origin {new}'


# meiva:note
# One host's `meiva stop`, with the flags the stop wave chose for it. `--yes` is always sent: the
# command runs over a non-interactive SSH.
def stop_command(flags: str) -> str:
    return f"{HOME}; {MEIVA} stop --yes {flags}"


# meiva:note
# Check NEW out detached, repair legacy read-only `.venv` directories (early converge passes left
# them 0555, which `uv sync` cannot write through), sync, and require a clean tree.
def switch_command(new: str) -> str:
    return (
        f"{HOME}; git checkout -q --detach {new} && {_VENV_DIRS} && "
        f"env -u VIRTUAL_ENV uv sync --frozen --compile-bytecode && {CLEAN}"
    )


def start_command(flags: str) -> str:
    return f"{HOME}; {MEIVA} start {flags}"


# meiva:note
# The login shell a host's command runs in: zsh on macOS, bash elsewhere, so the host's profile
# puts its own tools on PATH.
def login_shell(os_name: str, script: str) -> str:
    return f"{'zsh' if os_name == 'Darwin' else 'bash'} -lc {shlex.quote(script)}"


# meiva:note
# The helper facts of the commit the wave switches to, read by that commit's own module: the
# script and the package it imports are extracted from NEW's objects, because the installed
# checkout is the OLD commit and on a first wave does not carry this command at all.
def helper_probe_command(new: str) -> str:
    return (
        f"{HOME}\n"
        "tmp=$(mktemp -d)\n"
        f'git -C "$S" archive {shlex.quote(new)} | tar -x -C "$tmp"\n'
        'out=$(cd "$tmp" && PYTHONPATH="$tmp" "$S/.venv/bin/python" '
        '"$tmp/scripts/host_ops/tcc/helper-signing.py" facts 2>&1); rc=$?\n'
        'echo "h_rc=$rc"\n'
        "printf '%s\\n' \"$out\" | sed 's/^/h_/'\n"
        'rm -rf "$tmp"'
    )
