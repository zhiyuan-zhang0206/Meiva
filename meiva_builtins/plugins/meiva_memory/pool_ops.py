"""Shared helpers for the meiva-memory pool operation scripts.

Imported by the consolidation scripts under `skills/scripts/` (`consolidate.py`,
`steward.py`, `arbiter_merge.py`, `gen_indexes.py`, `rebuild_memory_index.py`)
as `meiva_builtins.plugins.meiva_memory.pool_ops`, so a run needs the checkout's
venv (`meiva_builtins` importable) — `consolidation/SKILL.md` invokes them with
a bare `python`, which an agent's shell resolves to that venv (the venv's
`bin/` leads the PATH every agent process inherits). Stdlib + subprocess plus
the leaf that resolves the home (`base.paths.home.resolve_meiva_home`:
`$MEIVA_HOME`, else `~/.meiva`), which builds no Settings, and `dotenv_values` to
read the home's `.env`.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from base.paths.home import resolve_meiva_home

RESERVED = {"MEMORY.md", "AGENTS.md", "index.md", "log.md"}


def pool_dir() -> Path:
    return resolve_meiva_home() / "memory"


def machine_name() -> str:
    """This host's machine name: `MEIVA_MACHINE_NAME` in the process environment, else
    in the home's `.env`. Builds no Settings.

    A host that declares neither is not a configured unit; the steward would push
    under a made-up branch, so this refuses instead.
    """
    env = os.environ.get("MEIVA_MACHINE_NAME", "").strip()
    if env:
        return env
    declared = (
        dotenv_values(resolve_meiva_home() / ".env").get("MEIVA_MACHINE_NAME") or ""
    ).strip()
    if declared:
        return declared
    raise SystemExit(
        "✗ MEIVA_MACHINE_NAME is set neither in the environment nor in "
        f"{resolve_meiva_home() / '.env'}; the memory branch is machine-<name>"
    )


def branch_name() -> str:
    return f"machine-{machine_name()}"


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
) -> subprocess.CompletedProcess:
    r = subprocess.run(  # noqa: S603 — caller-constructed argv, static args, not shell-interpolated
        cmd, capture_output=True, text=True, cwd=cwd, check=False
    )
    if r.stdout:
        print(r.stdout.rstrip())
    if check and r.returncode != 0:
        raise SystemExit(f"✗ {' '.join(cmd)} failed: {r.stderr.strip()}")
    return r


def _pool() -> Path:
    return Path(str(pool_dir()))


def frontmatter(rel: str) -> dict[str, Any]:
    with (_pool() / rel).open(encoding="utf-8") as f:
        content = f.read()
    m = re.match(r"^---\n(.*?)\n---\n", content, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def collect() -> list[str]:
    root = _pool()
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if ".git" in dirpath or ".githooks" in dirpath:
            continue
        for fn in filenames:
            if not fn.endswith(".md") or fn in RESERVED:
                continue
            rel = str((Path(dirpath) / fn).relative_to(root))
            files.append(rel)
    return sorted(files)


def _subdir_lines(dir_rel: str, dirs: list[str], all_files: list[str]) -> list[str]:
    lines: list[str] = []
    for d in dirs:
        prefix = (dir_rel + "/" if dir_rel else "") + d + "/"
        n = len([f for f in all_files if f.startswith(prefix)])
        lines.append(f"* [{d}/]({d}/) - {n} notes")
    return lines or ["*(none)*"]


def _note_lines(dir_rel: str, mds: list[str]) -> list[str]:
    lines: list[str] = []
    for md in mds:
        rel = (dir_rel + "/" if dir_rel else "") + md
        fm = frontmatter(rel)
        t = str(fm.get("title") or md[:-3])
        d = str(fm.get("description") or "").replace("\n", " ")
        lines.append(f"* [{t}]({md}) - {d}")
    return lines or ["*(none)*"]


def write_directory_index(dir_rel: str, all_files: list[str]) -> str:
    root = _pool()
    dpath = root / dir_rel if dir_rel else root
    entries = sorted(p.name for p in dpath.iterdir())
    dirs = [e for e in entries if (dpath / e).is_dir() and not e.startswith(".")]
    mds = [e for e in entries if e.endswith(".md") and e not in RESERVED]

    title = "(root)" if not dir_rel else dir_rel + "/"
    lines = [f"# {title}", "", "## Subdirectories", ""]
    lines.extend(_subdir_lines(dir_rel, dirs, all_files))
    lines.extend(["", "## Notes", ""])
    lines.extend(_note_lines(dir_rel, mds))
    lines.append("")

    target = dpath / "index.md" if dir_rel else root / "index.md"
    with target.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return str(target)


def generate_directory_indexes() -> list[str]:
    files = collect()
    dirs_with_notes: set[str] = set()
    for f in files:
        parent = Path(f).parent
        while str(parent) != ".":
            dirs_with_notes.add(str(parent))
            parent = parent.parent
        dirs_with_notes.add("")
    written = [
        write_directory_index(d, files)
        for d in sorted(dirs_with_notes, key=lambda x: (x.count("/"), x))
    ]
    print("index.md written:", len(written))
    return written


def _top_dir_detail(top: str, children: list[str], files: list[str]) -> str:
    n = sum(1 for f in files if f.startswith(top + "/"))
    if children == [top]:
        return f"{n} notes"
    counts = [
        f"{c.split('/')[-1]}({sum(1 for f in files if f.startswith(c + '/'))})"
        for c in children
        if c != top
    ]
    return f"{n} notes: {', '.join(counts)}"


def rebuild_root_memory_index() -> None:
    files = collect()
    root_files = [f for f in files if "/" not in f]
    sub: dict[str, list[str]] = {}
    for d in sorted({str(Path(rel).parent) for rel in files if "/" in rel}):
        sub.setdefault(d.split("/")[0], []).append(d)

    lines: list[str] = []
    for rel in root_files:
        fm = frontmatter(rel)
        title = str(fm.get("title") or Path(rel).name[:-3])
        desc = str(fm.get("description") or "").replace("\n", " ")
        lines.append(f"- [{title}]({rel}) \u2014 {desc}")
    for top in sorted(sub):
        lines.append(f"- [{top}/]({top}/index.md) \u2014 {_top_dir_detail(top, sub[top], files)}")

    memory_path = _pool() / "MEMORY.md"
    content = ""
    if memory_path.exists():
        with memory_path.open(encoding="utf-8") as f:
            content = f.read()
    heading = re.search(r"^## Pointers[ \t]*$", content, re.M)
    head = content[: heading.start()] if heading else (content.rstrip("\n") + "\n\n").lstrip("\n")
    new = head + "## Pointers\n\n\n" + "\n".join(lines) + "\n"
    print("root:", len(root_files), "dirs:", len(sub), "| chars:", len(new))
    with memory_path.open("w", encoding="utf-8") as f:
        f.write(new)


def regenerate_indexes() -> None:
    """Rebuild the pool's indexes: every directory `index.md` (`skills/scripts/gen_indexes.py`)
    and the `## Pointers` section of the root `MEMORY.md` (`skills/scripts/rebuild_memory_index.py`:
    one line per root note, one per top-level directory; the rest of the file is kept).

    Agents write notes with `meiva.files` and nothing maintains these indexes as they do, so the
    consolidation scripts rebuild them before staging: the pool validator's pointer checks
    (every note pointed at once, titles matching, no missing targets) pass on what gets committed."""
    generate_directory_indexes()
    rebuild_root_memory_index()


def stage_and_commit(message: str, pool: Path) -> bool:
    run(["git", "-C", str(pool), "add", "-A"])
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
