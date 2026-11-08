#!/usr/bin/env python3
# meiva:note
# check_narrative_facts.py — narrative-facts verifier (audit round-2 E12/P1-2).
#
# check_doc_references.py validates that docs point at things that exist
# (links, CLI flags). It cannot see that a doc's *prose* describes the code
# that is actually there. This script adds one narrative-fact check, extracted
# from the code and asserted against the doc that owns the fact:
#
#   A. skill catalog  — every slug-like skill name in docs/skills/skills.meiva.okf.md
#      functional-group rows must resolve under meiva_builtins/skills/ or
#      meiva_builtins/plugins/ (catches a removed skill still listed).
#
# The IM-narrative checks retired with the IM channels themselves (2026-11-08).
#
# Facts are declared once in code; the docs are views and this script is the
# freshness check. Run from the repo root; wired into pre-commit.

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _skill_group_names(doc: str) -> list[str]:
    names: list[str] = []
    for line in doc.splitlines():
        if not line.startswith("- [["):
            continue
        if " — " not in line:
            continue
        after = line.split(" — ", 1)[1]
        for token in after.split("/"):
            t = token.strip()
            if SLUG.match(t):
                names.append(t)
    return names


def check_skill_catalog() -> list[str]:
    doc_path = ROOT / "docs/skills/skills.meiva.okf.md"
    doc = doc_path.read_text(errors="replace")
    problems: list[str] = []
    for name in _skill_group_names(doc):
        if name in ("comms", "ops_lifecycle", "orchestration", "self_improvement", "web_media"):
            continue  # group nodes, not skills
        if not (
            any(d.is_dir() for d in (ROOT / "meiva_builtins/skills").glob(f"*/{name}"))
            or (ROOT / f"meiva_builtins/plugins/{name}").is_dir()
            or (ROOT / f"meiva_builtins/skills/{name}.meiva.okf.md").is_file()
        ):
            problems.append(
                f"docs/skills/skills.meiva.okf.md lists skill `{name}` but no "
                f"meiva_builtins/skills/<group>/{name}/ or meiva_builtins/plugins/{name}/ exists — "
                f"removed skill still in the catalog?"
            )
    return problems


def main() -> int:
    problems: list[str] = []
    problems += check_skill_catalog()
    if problems:
        print(f"check_narrative_facts: {len(problems)} narrative-fact violation(s)")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("check_narrative_facts: 0 violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
