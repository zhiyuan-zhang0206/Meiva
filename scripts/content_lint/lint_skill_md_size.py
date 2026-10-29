#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from base.host.env.boot_actions import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

from base.packages.text_budget import LengthBudget, Level, Unit, evaluate

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# meiva:link scripts/content_lint/docs/content_lint.meiva.okf.md
_HARD_CEILING = 300

_SKILL_MD_GLOBS = [
    "meiva_builtins/skills/*/*/SKILL.md",
    "meiva_builtins/skills/*/*/*/SKILL.md",
    "meiva_builtins/plugins/*/skills/SKILL.md",
    "meiva_builtins/plugins/*/skills/*/SKILL.md",
    ".agents/skills/*/SKILL.md",
    ".agents/skills/*/*/SKILL.md",
]


# meiva:link scripts/content_lint/docs/content_lint.meiva.okf.md
def _skill_md_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in _SKILL_MD_GLOBS:
        files.extend(root.glob(pattern))
    return sorted({p.resolve() for p in files})


# meiva:link scripts/content_lint/docs/content_lint.meiva.okf.md
def main(_argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    root = repo_root or _REPO_ROOT
    budget = LengthBudget(Unit.LINES, hard=_HARD_CEILING)
    exit_code = 0
    for path in _skill_md_files(root):
        rel = path.relative_to(root)
        verdict = evaluate(path.read_text(encoding="utf-8"), budget)
        n_lines = verdict.measured

        if verdict.level is Level.OVER:
            print(
                f"{rel}:{n_lines}: error: {n_lines} lines exceeds the {_HARD_CEILING}-line "
                f"ceiling. A skill is loaded whole and then held in context while it runs, so the "
                f"root carries the executable minimum — each step's command and its gate — while "
                f"explanation, variants, examples and the full failure list belong in "
                f"references/ or in a sub-skill. Move that section out. If the ceiling genuinely "
                f"needs to move, bump _HARD_CEILING in scripts/content_lint/lint_skill_md_size.py "
                f"in the same PR and justify it in the commit message. See "
                f"scripts/content_lint/docs/content_lint.meiva.okf.md.",
            )
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
