from __future__ import annotations

import sys
from pathlib import Path

from meizel.checkout import source_tree
from meizel.frontend_deps import FRONTEND_DIR, FrontendDeps, classify, observe, refusal


# meiva:note
# The question a caller asks in a process of its own: may an answer come from this tree, and if not,
# what does it say. The shell binds the tree this process has; the four probes and the verdict are
# `meizel.frontend_deps`'.
# meiva:link meizel/docs/frontend-deps.meiva.okf.md
def verdict(frontend: Path) -> tuple[FrontendDeps, str]:
    state = classify(observe(source_tree(frontend), frontend))
    return state, "" if state is FrontendDeps.READY else refusal(state)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    frontend = Path(arguments[0]) if arguments else Path.cwd().joinpath(*FRONTEND_DIR)
    state, message = verdict(frontend)
    if state is FrontendDeps.READY:
        print(f"frontend dependencies: ready ({frontend})")
        return 0
    print(f"meizel: {message}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
