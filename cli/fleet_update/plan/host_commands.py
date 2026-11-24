# meiva:note
# The shell text each host runs, as values. A window sends these over SSH unchanged: the hosts'
# own checkout, venv and `meiva` do the work, and this module names the order of their words.
# meiva:link cli/docs/fleet-update.md

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
grep -q -- --no-message "$S/cli/parsers/host.py" 2>/dev/null && echo stop_message=yes || echo stop_message=no
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
# stops, and pin `refs/fleet-update/target` to NEW so a runner whose `origin` is the gateway's
# checkout can fetch NEW by SHA over git-upload-pack even while the gateway checkout is detached.
def fetch_command(new: str) -> str:
    return (
        f'{HOME}; (git cat-file -e "{new}^{{commit}}" || git fetch -q origin {new}) '
        f"&& git update-ref refs/fleet-update/target {new}"
    )


# meiva:note
# One host's `meiva stop`, with the flags the stop wave chose for it. `--yes` is always sent: the
# command runs over a non-interactive SSH.
def stop_command(flags: str) -> str:
    return f"{HOME}; {MEIVA} stop --yes {flags}"


FRONTEND_STAGE = '"$H/frontend-prebuild"'


def adopt_frontend_build(new: str) -> str:
    """Move the frontend build prestage staged for exactly NEW into the checkout just switched to.

    `meiva start` then finds `.next/meiva-build-key` current and skips `next build`. Only a stage
    whose worktree is at NEW and that holds a finished build (the key is written after a
    successful build) is taken; otherwise nothing changes and the start builds as before, which
    is also what the recovery switch back to OLD does. The stage (a git worktree of NEW beside the
    serving checkout, on the same filesystem so the move is one rename) is not deleted here,
    because that would spend seconds of the outage: the next prestage removes it. Always succeeds.
    """
    return (
        f"(W={FRONTEND_STAGE}; "
        f'if [ "$(git -C "$W" rev-parse HEAD 2>/dev/null)" = {new} ] '
        '&& [ -f "$W/ui/web/.next/meiva-build-key" ]; then '
        'rm -rf "$S/ui/web/.next" && mv "$W/ui/web/.next" "$S/ui/web/.next" '
        '|| echo "frontend prebuild not adopted: meiva start builds the frontend"; fi; true)'
    )


# meiva:note
# Check NEW out detached, repair legacy read-only `.venv` directories (early converge passes left
# them 0555, which `uv sync` cannot write through), sync (reinstalling the editable `meiva` wheel so
# `force-include` trees like `schedules/` and `migrations/` in `site-packages` refresh even when
# `uv.lock` is unchanged), and require a clean tree.
def switch_command(new: str) -> str:
    return (
        f"{HOME}; git checkout -q --detach {new} && {adopt_frontend_build(new)} && {_VENV_DIRS} && "
        f"env -u VIRTUAL_ENV uv sync --frozen --compile-bytecode --reinstall-package meiva && {CLEAN}"
    )


def start_command(flags: str) -> str:
    return f"{HOME}; {MEIVA} start {flags}"


# meiva:note
# The login shell a host's command runs in: zsh on macOS, bash elsewhere, so the host's profile
# puts its own tools on PATH.
def login_shell(os_name: str, script: str) -> str:
    return f"{'zsh' if os_name == 'Darwin' else 'bash'} -lc {shlex.quote(script)}"
