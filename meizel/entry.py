# meiva:note
# The checker's process entry: the only code that asks where this checkout and this installation
# are. It parses one argv, opens the single repository handle every command is handed, and gives
# the command layer the two readers only a shell can hold (the roles manifest and a file the argv
# names). Everything below it takes those values.
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from meizel.checkout import GitRepo, read_roles, read_text, source_tree, toplevel
from meizel.contracts import cli
from meizel.frontend_deps import FrontendDepsError
from meizel.graph.model import MeizelError

__all__ = ["main", "open_repository"]

# meiva:note
# The installation this process runs from: the one seat that answers where the checker's own files
# are, read once here and handed down as a value (`source_evidence.Evidence`).
_TOOL_ROOT = Path(__file__).resolve().parents[1]


# meiva:note
# The repository handle the commands are handed, over `root`. The entry owns this seat: a caller
# that needs the same handle (a test driving the CLI) opens it here instead of building its own.
def open_repository(root: Path) -> GitRepo:
    return GitRepo(root)


# meiva:note
# A target as argv typed it, resolved against the directory this entry resolved — the repository
# when `--repo` names one, the working directory otherwise — and expressed relative to `root`. Only
# the entry may resolve: a command below it holds neither the working directory nor the checkout
# root, so resolving there would repeat the read in every one of its seven call sites.
def relative_targets(root: Path, base: Path) -> Callable[[str], str]:
    def relative(raw: str) -> str:
        resolved = (base / raw).resolve()
        try:
            rel = resolved.relative_to(root)
        except ValueError as error:
            raise MeizelError(f"{raw}: outside the repository {root}") from error
        return "" if str(rel) == "." else rel.as_posix()

    return relative


# meiva:note
# The two namespace fields the entry reads. `parse_args` returns an `argparse.Namespace`, whose
# attributes are `Any`, so they are read once into typed values through the checker's own reader
# (`contracts.cli.read_args`, which also refuses a value of the wrong type).
@dataclass(frozen=True)
class _EntryOptions:
    jobs: int | None
    repo: Path | None


# meiva:note
# Run one checker command from argv and return the process exit status.
def main(argv: list[str]) -> int:
    args = cli.build_parser().parse_args(argv)
    options = cli.read_args(args, _EntryOptions)
    # meiva:note
    # The one place the processor count is read: below the entry, `jobs=None` is serial.
    jobs = options.jobs if options.jobs is not None else min(os.cpu_count() or 1, 8)
    args.jobs = jobs
    try:
        base = options.repo if options.repo is not None else Path.cwd()
        # meiva:note
        # The one repository seat of the command: this checkout, read through one value that the
        # commands are handed instead of a path they would build their own from. A command that
        # judges reads a snapshot of it (`repo.universe()`).
        repo = open_repository(options.repo if options.repo is not None else toplevel(base))
        root = repo.root.resolve()
        deps = cli.CommandDeps(
            tool_root=_TOOL_ROOT,
            read_roles=read_roles,
            read_text=read_text,
            relative_target=relative_targets(root, base),
            source_tree=source_tree,
            today=datetime.now(UTC).date().isoformat(),
        )
        return cli.dispatch(repo, root, base, args, deps)
    except FrontendDepsError as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 3
    except MeizelError as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2
