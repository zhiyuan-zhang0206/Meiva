from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from meizel.api import GitRepo, MeizelError, tracked_files


def child_environment(parent: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in parent.items() if not name.startswith("MEIVA_")}


def git(root: Path, *argv: str) -> str:
    return subprocess.check_output(["git", *argv], cwd=root, text=True).strip()  # noqa: S603


def index_scan_problem(root: Path) -> str | None:
    try:
        repo = GitRepo(root)
        algorithm = git(root, "rev-parse", "--show-object-format")
        dirty = set(repo.run_bytes("diff-files", "--name-only", "-z", "--").split(b"\0"))
        regular = set(tracked_files(root, regular=True))
        for record in repo.run_bytes("ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            header, name = record.split(b"\t", 1)
            _, indexed, stage = header.split()
            path = name.decode()
            if stage != b"0" or name in dirty or path not in regular:
                continue
            data = (root / path).read_bytes()
            digest = hashlib.new(algorithm, f"blob {len(data)}\0".encode())
            digest.update(data)
            if digest.hexdigest().encode() != indexed:
                return (
                    f"meizel: working-tree bytes differ from the index for {path!r}, "
                    "which git diff-files reports as clean. Update the file mtime and "
                    "stage the intended edit before retrying."
                )
    except (OSError, ValueError, subprocess.SubprocessError, MeizelError) as error:
        return f"meizel: could not verify scan input against the Git index: {error}"
    return None


def main(root: Path, *, python: str, environment: Mapping[str, str]) -> int:
    if (problem := index_scan_problem(root)) is not None:
        print(problem, file=sys.stderr)
        return 1
    ran = subprocess.run(  # noqa: S603
        [python, "-m", "meizel", "check"], cwd=root, env=dict(environment), check=False
    )
    return ran.returncode


if __name__ == "__main__":
    raise SystemExit(
        main(Path.cwd(), python=sys.executable, environment=child_environment(os.environ))
    )
