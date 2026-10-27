"""Profile provisioning for the shared headed Chrome (meiva-browser).

The browser daemon runs Chrome against a *dedicated* profile at
`$MEIVA_HOME/chrome-profile`, isolated from the operator's daily Chrome. On a
fresh install that profile is empty, so the agent must sign in to every site
from scratch. This module offers the alternative at first-start time: seed the
dedicated profile by COPYING the operator's daily Chrome profile, so the agent
inherits every logged-in session and acts as the user without re-authenticating.

Copying hands the agent the user's full logged-in identity (cookies, sessions,
saved passwords, signed-in accounts), so it is a SECURITY decision. It is made
only interactively and only when the dedicated profile does not yet exist — any
existing profile directory is NEVER touched (constraint: prod carries a multi-GB
logged-in profile that must survive every `meiva start`).

`ensure_browser_profile` is called from the converge `_ensure_browser` step,
which runs at `meiva start` before the browser session launches. Non-interactive
paths (watchdog respawn, boot autostart, the fleet update rollout) pass
`interactive=False` and take the fresh-profile default without prompting.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from base.host.proc import process_alive

# meiva:note
# Names never worth copying out of a Chrome user-data dir. `Singleton*` are the
# live lock/socket/cookie files Chrome holds while running — copying them would
# import a stale lock that confuses the agent's Chrome. The rest are large,
# regenerable caches (GPU/shader/HTTP/service-worker) that only bloat the copy;
# Chrome rebuilds them on demand. Everything else — cookies, Login Data, Local
# State, per-profile prefs — is copied so the agent inherits the logged-in state.
_EXCLUDED_NAMES = frozenset(
    {
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "CacheStorage",
        "ScriptCache",
        "Crashpad",
        "component_crx_cache",
    }
)


# meiva:note
# The agent's dedicated Chrome profile (`<home>/chrome-profile`): the single source of
# truth shared by the daemon (which launches Chrome against it) and this module (which
# may seed it).
def profile_dir(home: Path) -> Path:
    return home / "chrome-profile"


def profile_is_populated(path: Path) -> bool:
    """True when `path` is a non-empty directory — an existing profile to leave
    untouched. A missing or empty dir counts as fresh (eligible for seeding)."""
    return path.is_dir() and any(path.iterdir())


def ignore_junk(_src: str, names: list[str]) -> set[str]:
    """`shutil.copytree` ignore callback: drop lock/socket files and regenerable
    caches (see `_EXCLUDED_NAMES`)."""
    return {n for n in names if n.startswith("Singleton") or n in _EXCLUDED_NAMES}


def dir_size_bytes(src: Path) -> int:
    """Size (bytes) of what a copy would carry: walk `src` applying the same
    exclusions `copy_default_profile` uses, so the reported figure matches the
    bytes actually copied. Symlinks are not followed (their target isn't copied)."""
    total = 0
    for root, dirs, files in os.walk(src):
        ignored = ignore_junk(root, dirs + files)
        dirs[:] = [d for d in dirs if d not in ignored]  # prune -> os.walk won't descend
        for name in files:
            if name in ignored:
                continue
            try:
                total += (Path(root) / name).stat(follow_symlinks=False).st_size
            except OSError:
                continue  # vanished/unreadable mid-walk — not worth failing a size estimate
    return total


def running_chrome_pid(src: Path) -> int | None:
    """Pid of a Chrome holding `src` open, or None if none is.

    Chrome writes a `SingletonLock` symlink -> `<hostname>-<pid>` while a
    user-data dir is open. A live pid means the SQLite stores (Cookies, Login
    Data) may be mid-write, so copying now risks a corrupt import — the caller
    refuses until Chrome is quit. A stale lock (process gone after a crash) or a
    non-symlink/missing lock reads as 'not running'. The pid is trusted over a
    hostname match: a false 'running' (harmless retry prompt) is preferable to a
    false 'not running' (a corrupt copy)."""
    try:
        target = (src / "SingletonLock").readlink()
    except OSError:
        return None  # no lock, or not a symlink -> not running
    pid_str = str(target).rpartition("-")[2]
    if not pid_str.isdigit():
        return None
    pid = int(pid_str)
    # meiva:note
    # `process_alive`, not a raw `os.kill(pid, 0)`: a recycled pid owned by
    # another user still counts as alive (running).
    if process_alive(pid):
        return pid
    return None  # stale lock


def copy_default_profile(src: Path, dst: Path) -> None:
    """Copy the daily Chrome user-data dir `src` into the agent profile `dst`,
    excluding locks + caches. `dst` must be absent: any existing directory is an
    immutable Chrome profile, including a partial first copy, so it is never
    overwritten or cleared automatically."""
    if dst.exists():
        raise FileExistsError(f"refusing to overwrite existing Chrome profile: {dst}")
    # meiva:note
    # symlinks=True copies links verbatim rather than following them, so a
    # broken link in the source can't abort the whole copy. If this fails after
    # creating `dst`, leave it intact rather than deleting potentially valuable
    # profile material; a human can inspect or remove it explicitly.
    shutil.copytree(src, dst, ignore=ignore_junk, symlinks=True)


# meiva:note
# Return a warning for unsafe-looking `Local State`, without reading it.
#
# Chrome owns this file's encrypted metadata. Startup only validates its existence, ordinary read
# permission, and mtime (against `now`, epoch seconds); it never parses, copies, rewrites, or
# deletes it. A warning remains non-fatal because Chrome is the only component that can
# authoritatively interpret its own state.
def validate_local_state(profile: Path, *, now: float) -> str | None:
    local_state = profile / "Local State"
    try:
        metadata = local_state.stat()
    except FileNotFoundError:
        return f"Local State is missing at {local_state}"
    except OSError as exc:
        return f"Local State could not be inspected at {local_state}: {exc}"
    if not metadata.st_mode & 0o444 or not os.access(local_state, os.R_OK):
        return f"Local State is not readable at {local_state}"
    if metadata.st_mtime > now + 300:
        return f"Local State mtime is unexpectedly in the future at {local_state}"
    return None


def human_size(n_bytes: int) -> str:
    gb = n_bytes / 1024**3
    return f"{gb:.1f} GB" if gb >= 1 else f"{n_bytes / 1024**2:.0f} MB"


# meiva:note
# The two streams a prompt talks over: `say` writes a line to `stdout`, `ask` writes the prompt
# and reads one line from `stdin` (end of input is an `EOFError`, as with `input()`). The process
# entry builds the real one (`Console.of_process()`); a test hands in `io.StringIO` streams.
@dataclass(frozen=True)
class Console:
    stdin: TextIO
    stdout: TextIO

    @classmethod
    def of_process(cls) -> Console:
        return cls(stdin=sys.stdin, stdout=sys.stdout)

    def say(self, text: str = "") -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def ask(self, prompt: str) -> str:
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if not line:
            raise EOFError("no answer: the input stream ended")
        return line.rstrip("\n")


def _prompt_profile_choice(src: Path, console: Console) -> str:
    """Explain the choice and return 'copy' or 'new' (default 'new')."""
    console.say("\n  The agent's browser uses a DEDICATED Chrome profile, separate from your")
    console.say("  daily Chrome. Fresh, it is empty — the agent signs in to every site itself.")
    console.say("")
    console.say(f"  You can instead COPY your daily Chrome profile ({src}) into it. The agent")
    console.say("  then inherits every logged-in session and acts on any site you are signed")
    console.say("  into — using your cookies, saved passwords, and accounts — without a re-login.")
    console.say("")
    console.say("  SECURITY: a copied profile gives the agent your full logged-in identity;")
    console.say("  anything it does in the browser is done as you.")
    answer = console.ask(
        "  Seed the browser profile from your daily Chrome? [copy / new] (default new): "
    )
    return "copy" if answer.strip().lower() in ("copy", "c") else "new"


def _confirm_copy(src: Path, dst: Path, size_bytes: int, console: Console) -> bool:
    """Final size-aware confirmation for the copy. Returns True only on explicit yes."""
    console.say(f"\n  This copies {human_size(size_bytes)} from")
    console.say(f"    {src}")
    console.say("  into the agent's profile at")
    console.say(f"    {dst}")
    console.say("  The agent will then be logged into everything you are logged into.")
    return console.ask("  Type 'yes' to confirm the copy: ").strip().lower() in ("yes", "y")


# meiva:note
# Converge passes the daily profile candidate selected from its captured session home and
# platform. Probe it only after preserving existing destinations and noninteractive starts.
def ensure_browser_profile(
    *, interactive: bool, daily_profile: Path, profile: Path, console: Console
) -> None:
    """Offer, once, to seed the agent's dedicated Chrome profile from the daily one.

    No-ops (leaving the daemon to create a fresh empty profile) when the profile
    path already exists, when not interactive, or when this host has no daily
    Chrome profile to copy from. Otherwise prompts; on 'copy' it refuses while
    Chrome is still running (retry loop), reports the size, takes a final
    confirmation, then copies."""
    if profile.exists():
        return  # never clobber an existing profile
    if not interactive:
        return  # non-interactive default = fresh empty profile
    src = daily_profile
    if not src.is_dir():
        return  # no daily Chrome to copy from -> fresh profile, no prompt

    if _prompt_profile_choice(src, console) != "copy":
        return

    # meiva:note
    # Copying a live profile risks importing half-written SQLite. Require Chrome
    # closed; loop so the operator can quit it and continue without restarting.
    while running_chrome_pid(src) is not None:
        console.say("\n  Chrome is still running — copying now could corrupt the imported data.")
        if (
            console.ask("  Quit Chrome fully, then press Enter to retry (or type 'new'): ")
            .strip()
            .lower()
            == "new"
        ):
            return

    if not _confirm_copy(src, profile, dir_size_bytes(src), console):
        return
    console.say(f"  Copying your Chrome profile into {profile} … (this may take a while)")
    copy_default_profile(src, profile)
    console.say("  Done — the agent's browser now shares your logged-in Chrome state.")
