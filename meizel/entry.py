# meiva:note
# The checker's process entry: the only code that asks where this checkout and this installation
# are. It parses one argv, opens the single repository handle every command is handed, and gives
# the command layer the two readers only a shell can hold (the roles manifest and a file the argv
# names). Everything below it takes those values.
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from meizel.checkout import GitRepo, read_roles, read_text, toplevel
from meizel.contracts import cli
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
# Run one checker command from argv and return the process exit status.
def main(argv: list[str] | None = None) -> int:
    args = cli.build_parser().parse_args(argv)
    try:
        base = args.repo if args.repo is not None else Path.cwd()
        # meiva:note
        # The one repository seat of the command: this checkout, read through one value that the
        # commands are handed instead of a path they would build their own from. A command that
        # judges reads a snapshot of it (`repo.universe()`).
        repo = open_repository(args.repo if args.repo is not None else toplevel(base))
        deps = cli.CommandDeps(
            tool_root=_TOOL_ROOT,
            read_roles=read_roles,
            read_text=read_text,
            today=datetime.now(UTC).date().isoformat(),
        )
        return cli.dispatch(repo, repo.root.resolve(), base, args, deps)
    except MeizelError as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2
