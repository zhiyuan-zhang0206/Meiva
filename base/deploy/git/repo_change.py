"""Repo change classification — which side (frontend / backend) a set of changed
file paths touches.

CI's change-class gating (the `classify` job in `.github/workflows/ci.yml`) and the
PR test selector (`scripts/ci/test_selector.py`) both classify a diff through it.
"""

from __future__ import annotations

# meiva:note
# A changelog fragment under `.changes/` is an entry waiting for the root
# CHANGELOG, so it is documentation: a fragment-only diff selects no backend lane
# and the path has an owner in the test selector.
# meiva:link docs/conventions/changelog.md
_DOC_ROOTS = (
    ".changes/",
    "docs/",  # project documentation, including contributor guidance and frozen history
    "future/",  # the plans axis
    "assets/",  # README-embedded artifacts
    "schedules/",  # version-controlled schedule templates (provisioned, not imported)
)


# meiva:note
# A test under a doc root is code: schedules/tests/ is collected by pytest, so a
# change there must select the backend lane, while the templates beside it stay
# documentation.
def is_doc_path(path: str) -> bool:
    """Documentation axes, top-level Markdown and component OKF doc layers.

    Test data is excluded from the component rule. Other nested Markdown
    (e.g. ui/web/AGENTS.md or SKILL.md) is classified by its code directory.
    """
    directories = path.split("/")[:-1]
    return (
        (path.startswith(_DOC_ROOTS) and "tests" not in directories)
        or (path.endswith(".md") and "/" not in path)
        or (path.endswith(".meiva.okf.md") and "docs" in directories and "tests" not in directories)
    )


def classify_change(paths: list[str]) -> tuple[bool, bool]:
    """Map changed file paths to (frontend_changed, backend_changed).

    frontend = non-docs under `ui/web/`; backend = other non-document paths.
    A docs-only (or empty) diff yields (False, False); CI retains its independent
    documentation gates and applies its event-specific full-suite policy.
    """
    frontend = backend = False
    for p in paths:
        if is_doc_path(p):
            continue
        if p.startswith("ui/web/"):
            frontend = True
        else:
            backend = True
    return frontend, backend
