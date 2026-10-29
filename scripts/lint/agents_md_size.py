from __future__ import annotations

import sys
from pathlib import Path

from base.host.env.boot_actions import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

from base.packages.text_budget import LengthBudget, Level, Unit, evaluate

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# meiva:link scripts/lint/docs/agents-md-size.meiva.okf.md
_HARD_CEILING = 245


# meiva:link scripts/lint/docs/agents-md-size.meiva.okf.md
def main(_argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    agents_md = (repo_root or _REPO_ROOT) / "AGENTS.md"
    if not agents_md.exists():
        print(f"error: {agents_md} not found", file=sys.stderr)
        return 1

    verdict = evaluate(
        agents_md.read_text(encoding="utf-8"), LengthBudget(Unit.LINES, hard=_HARD_CEILING)
    )
    n_lines = verdict.measured

    if verdict.level is Level.OVER:
        print(
            f"AGENTS.md:{n_lines}: error: {n_lines} lines exceeds the {_HARD_CEILING}-line "
            f"ceiling. AGENTS.md is the first file every agent reads, so it carries the rules that "
            f"change how they work and links to the rest; detail that matters only while doing one "
            f"task belongs in docs/conventions/, where it is read on demand. Move that section out. "
            f"If the ceiling genuinely needs to move, bump _HARD_CEILING in "
            f"scripts/lint/agents_md_size.py in the same PR and justify it in the commit message. "
            f"See scripts/lint/docs/agents-md-size.meiva.okf.md.",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
