# meiva:note
# Shared helpers for the meiva-memory pool operation scripts.
#
# Imported by the consolidation scripts under `skills/scripts/` (`consolidate.py`,
# `steward.py`, `arbiter_merge.py`, `gen_indexes.py`, `rebuild_memory_index.py`)
# as `meiva_builtins.plugins.meiva_memory.pool_ops`, so a run needs the checkout's
# venv (`meiva_builtins` importable) — `consolidation/SKILL.md` invokes them with
# a bare `python`, which an agent's shell resolves to that venv (the venv's
# `bin/` leads the PATH every agent process inherits). Stdlib + subprocess plus
# `dotenv_values` to read the home's `.env`; every helper takes the home or the pool
# its script resolved at its entry (`base.paths.home.resolve_meiva_home`:
# `$MEIVA_HOME`, else `~/.meiva`, which builds no Settings).

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml
from dotenv import dotenv_values

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.paths import memory_dir

RESERVED = {"MEMORY.md", "AGENTS.md", "index.md", "log.md"}


# meiva:note
# The memory pool of the Meiva home `home`: `base.paths.memory_dir`, the owner of that path.
def pool_dir(home: Path) -> Path:
    return memory_dir(home=home)


# meiva:note
# This host's machine name: `MEIVA_MACHINE_NAME` in `environ`, else in `home`'s `.env`. Builds no
# Settings. A host that declares neither is not a configured unit; the steward would push under a
# made-up branch, so this refuses instead. `environ` is the process environment its entry script
# read once -- the steward passes its own, so this helper reads none.
def machine_name(home: Path, *, environ: Mapping[str, str]) -> str:
    env = environ.get("MEIVA_MACHINE_NAME", "").strip()
    if env:
        return env
    declared = (dotenv_values(home / ".env").get("MEIVA_MACHINE_NAME") or "").strip()
    if declared:
        return declared
    raise SystemExit(
        "✗ MEIVA_MACHINE_NAME is set neither in the environment nor in "
        f"{home / '.env'}; the memory branch is machine-<name>"
    )


def branch_name(home: Path, *, environ: Mapping[str, str]) -> str:
    return f"machine-{machine_name(home, environ=environ)}"


def current_branch(pool: Path) -> str:
    """The branch checked out in the pool ("" when HEAD is detached)."""
    r = subprocess.run(  # noqa: S603 — fixed git argv, static args, not shell-interpolated
        ["git", "-C", str(pool), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise SystemExit(f"✗ cannot read current branch: {r.stderr.strip()}")
    return r.stdout.strip()


def repo_slug(pool: Path) -> str:
    """user/repo from the pool's origin remote."""
    r = subprocess.run(  # noqa: S603 — fixed git argv, static args, not shell-interpolated
        ["git", "-C", str(pool), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise SystemExit(f"✗ cannot read origin remote: {r.stderr.strip()}")
    url = r.stdout.strip()
    # git@github.com:user/repo.git | https://github.com/user/repo.git
    return url.rstrip("/").rstrip(".git").split("github.com", 1)[-1].strip(":/")


def run(
    cmd: list[str], cwd: str | None = None, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(  # noqa: S603 — caller-constructed argv, static args, not shell-interpolated
        cmd, capture_output=True, text=True, cwd=cwd, check=False
    )
    if r.stdout:
        print(r.stdout.rstrip())
    if check and r.returncode != 0:
        raise SystemExit(f"✗ {' '.join(cmd)} failed: {r.stderr.strip()}")
    return r


def frontmatter(pool: Path, rel: str) -> dict[str, object]:
    with (pool / rel).open(encoding="utf-8") as f:
        content = f.read()
    m = re.match(r"^---\n(.*?)\n---\n", content, re.S)
    if not m:
        return {}
    try:
        loaded = object_of(yaml.safe_load, m.group(1))
    except yaml.YAMLError as error:
        raise ValueError(f"{rel}: frontmatter is not valid YAML: {error}") from error
    if loaded is None:
        return {}
    if not is_object_dict(loaded):
        raise ValueError(f"{rel}: frontmatter must be a mapping, got {type(loaded).__name__}")
    return loaded


# meiva:note
# The pool's notes: the `.md` paths of its git index minus `RESERVED`, in sorted order. Outside a
# git checkout (or when `pool` is not the checkout's root) the filesystem is walked instead.
#
# Why the index and not the working tree (2026-11-06): the pool validator enumerates
# `git ls-files` -- deliberately, so an untracked draft cannot block anybody's commit -- and this
# generator enumerated the working tree. One derived number, two sources, two opposite failures.
# An untracked draft inflated N here and wrote a pointer line to a file that never reached HEAD
# (silent). Once the validator started checking N, the same draft also turned every commit red
# (loud). Reading the index here removes both: a draft is invisible to the two sides alike, and a
# `git add`-ed but uncommitted note is visible to both, so "stage before generating" stops being a
# procedure one has to remember and becomes a property. A scheduled run over a fully tracked home
# checkout sees no change at all.
def collect(pool: Path) -> list[str]:
    tracked = _tracked_notes(pool)
    return tracked if tracked is not None else _walk_notes(pool)


def _tracked_notes(pool: Path) -> list[str] | None:
    prefix = subprocess.run(  # noqa: S603
        ["git", "-C", str(pool), "rev-parse", "--show-prefix"],
        capture_output=True,
        text=True,
        check=False,
    )
    if prefix.returncode != 0 or prefix.stdout.strip():
        # meiva:note
        # Not a checkout, or a subdirectory of one: `ls-files` would then print paths from the
        # enclosing repository's root, which are not this pool's to count.
        return None
    listed = subprocess.run(  # noqa: S603
        ["git", "-C", str(pool), "ls-files", "-z", "--", "*.md"],
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        return None
    names = [n for n in listed.stdout.decode("utf-8", "replace").split("\0") if n]
    return sorted(n for n in names if Path(n).name not in RESERVED and (pool / n).is_file())


def _walk_notes(pool: Path) -> list[str]:
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(pool):
        if ".git" in dirpath or ".githooks" in dirpath:
            continue
        for fn in filenames:
            if not fn.endswith(".md") or fn in RESERVED:
                continue
            files.append(str((Path(dirpath) / fn).relative_to(pool)))
    return sorted(files)


def _subdir_lines(dir_rel: str, dirs: list[str], all_files: list[str]) -> list[str]:
    lines: list[str] = []
    for d in dirs:
        prefix = (dir_rel + "/" if dir_rel else "") + d + "/"
        n = len([f for f in all_files if f.startswith(prefix)])
        lines.append(f"* [{d}/]({d}/) - {n} notes")
    return lines or ["*(none)*"]


def _note_lines(pool: Path, dir_rel: str, mds: list[str]) -> list[str]:
    lines: list[str] = []
    for md in mds:
        rel = (dir_rel + "/" if dir_rel else "") + md
        fm = frontmatter(pool, rel)
        t = str(fm.get("title") or md[:-3])
        d = str(fm.get("description") or "").replace("\n", " ")
        lines.append(f"* [{t}]({md}) - {d}")
    return lines or ["*(none)*"]


def write_directory_index(pool: Path, dir_rel: str, all_files: list[str]) -> str:
    root = pool
    dpath = root / dir_rel if dir_rel else root
    prefix = dir_rel + "/" if dir_rel else ""
    # meiva:note
    # Both lists come from `all_files` -- the same notes `collect` enumerated -- and never from
    # `iterdir()` (2026-11-06). A directory listing is the working tree, and the working tree is
    # not what a reader of the committed index gets: an untracked draft there produced a pointer
    # line to a file that never reached the commit, and nothing reported it. Deriving the notes
    # and the subdirectories from the enumerated set keeps one source; a working tree carries
    # directories git cannot store (`__pycache__`, an editor scratch dir) and listing one as
    # "0 notes" made a single pool produce different indexes on different machines.
    below = [f[len(prefix) :] for f in all_files if f.startswith(prefix)]
    sdirs = {rest.split("/", 1)[0] for rest in below if "/" in rest}
    dirs = sorted(sdirs)
    mds = [rest for rest in below if "/" not in rest]

    title = "(root)" if not dir_rel else dir_rel + "/"
    lines = [f"# {title}", "", "## Subdirectories", ""]
    lines.extend(_subdir_lines(dir_rel, dirs, all_files))
    lines.extend(["", "## Notes", ""])
    lines.extend(_note_lines(pool, dir_rel, mds))
    lines.append("")

    target = dpath / "index.md" if dir_rel else root / "index.md"
    with target.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return str(target)


def generate_directory_indexes(pool: Path) -> list[str]:
    files = collect(pool)
    dirs_with_notes: set[str] = set()
    for f in files:
        parent = Path(f).parent
        while str(parent) != ".":
            dirs_with_notes.add(str(parent))
            parent = parent.parent
        dirs_with_notes.add("")
    written = [
        write_directory_index(pool, d, files)
        for d in sorted(dirs_with_notes, key=lambda x: (x.count("/"), x))
    ]
    print("index.md written:", len(written))
    return written


# meiva:note
# The root index's line format has two test owners on purpose -- an end-to-end one that pins
# the whole file for a tiny pool, and a unit one that pins individual lines:
# `skills/scripts/tests/test_rebuild_memory_index.py` and
# `tests/test_meiva_memory_pool_index.py`. Changing the format means changing both; the
# 2026-11-02 change (dropping the per-subdirectory listing) was green on the second and red
# on the first, which cost a round trip through CI. Neither test is redundant: the end-to-end
# one is the only place the file-level contract (heading, ordering, trailing newline) lives.
# meiva:link meiva_builtins/plugins/meiva_memory/skills/scripts/rebuild_memory_index.py
def rebuild_root_memory_index(pool: Path) -> None:
    files = collect(pool)
    root_files = [f for f in files if "/" not in f]
    tops = {rel.split("/")[0] for rel in files if "/" in rel}

    lines: list[str] = []
    for rel in root_files:
        fm = frontmatter(pool, rel)
        title = str(fm.get("title") or Path(rel).name[:-3])
        desc = str(fm.get("description") or "").replace("\n", " ")
        lines.append(f"- [{title}]({rel}) \u2014 {desc}")
    for top in sorted(tops):
        n = sum(1 for f in files if f.startswith(top + "/"))
        lines.append(f"- [{top}/]({top}/index.md) \u2014 {n} notes")

    memory_path = pool / "MEMORY.md"
    content = ""
    if memory_path.exists():
        with memory_path.open(encoding="utf-8") as f:
            content = f.read()
    heading = re.search(r"^## Pointers[ \t]*$", content, re.M)
    head = content[: heading.start()] if heading else (content.rstrip("\n") + "\n\n").lstrip("\n")
    new = head + "## Pointers\n\n\n" + "\n".join(lines) + "\n"
    print("root:", len(root_files), "dirs:", len(tops), "| chars:", len(new))
    with memory_path.open("w", encoding="utf-8") as f:
        f.write(new)


# meiva:note
# Rebuild `pool`'s indexes: every directory `index.md` (`skills/scripts/gen_indexes.py`)
# and the `## Pointers` section of the root `MEMORY.md` (`skills/scripts/rebuild_memory_index.py`:
# one line per root note, one per top-level directory; the rest of the file is kept).
#
# Agents write notes with `meiva.files` and nothing maintains these indexes as they do, so the
# consolidation scripts rebuild them: the pool validator's pointer checks (every note pointed at
# once, titles matching, no missing targets) pass on what gets committed.
#
# Call `stage` first (2026-11-06): the rebuild enumerates the git index, so a note that is only in
# the working tree is not part of the pool yet and would be left out of the very commit it is
# about to enter. `stage_and_commit` stages again after the rebuild, which is what carries the
# rewritten indexes into the commit.
def regenerate_indexes(pool: Path) -> None:
    generate_directory_indexes(pool)
    rebuild_root_memory_index(pool)


def stage(pool: Path) -> None:
    run(["git", "-C", str(pool), "add", "-A"])


def stage_and_commit(message: str, pool: Path) -> bool:
    stage(pool)
    status = run(["git", "-C", str(pool), "status", "--porcelain"], check=False)
    if not status.stdout.strip():
        print("  (nothing to commit)")
        return False
    run(["git", "-C", str(pool), "commit", "-m", message])
    print(f"  committed: {message}")
    return True


def refresh_index() -> None:
    """Refresh the gateway's memory index via the CLI (the one memory CLI that stays)."""
    run(["meiva", "memory", "refresh"])
