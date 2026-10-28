from pathlib import Path

import pytest
import yaml

from base.host.proc import run_bounded
from scripts.audit.analysis.frozen import is_frozen
from scripts.content_lint import (
    check_doc_references,
    lint_doc_anchors,
    lint_doc_roster,
    lint_doc_symbols,
)

_REPO = Path(__file__).resolve().parents[3]


def test_procedural_scan_roots_follow_relocated_conventions() -> None:
    expected = _REPO / "docs" / "conventions"
    assert expected == lint_doc_anchors.DOCS_CONVENTIONS
    assert expected == lint_doc_symbols.DOCS_CONVENTIONS
    assert (
        _REPO / "services" / "supervision" / "roster" / "docs" / "service-roster.md"
        == lint_doc_roster.SERVICE_ROSTER
    )


@pytest.mark.parametrize("axis", ["decisions", "postmortems"])
def test_relocated_history_is_exempt_but_live_docs_still_fail(tmp_path: Path, axis: str) -> None:
    history = tmp_path / "docs" / axis / "record.md"
    history.parent.mkdir(parents=True)
    history.write_text("See [the old implementation](missing.py).\n")
    live = tmp_path / "docs" / "conventions" / "guide.md"
    live.parent.mkdir(parents=True)
    live.write_text("See [the implementation](missing.py).\n")
    run_bounded(["git", "init", "-q", str(tmp_path)], timeout=10).check_returncode()
    run_bounded(["git", "-C", str(tmp_path), "add", "."], timeout=10).check_returncode()
    assert check_doc_references.main(repo=tmp_path, commands={}, docs=[history]) == 0
    assert check_doc_references.main(repo=tmp_path, commands={}, docs=[live]) == 1
    assert is_frozen(history.relative_to(tmp_path).as_posix())
    assert not is_frozen(live.relative_to(tmp_path).as_posix())


def test_relocated_conventions_trigger_procedural_hooks() -> None:
    import re

    config = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text())
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    assert re.search(
        hooks["lint-doc-roster"]["files"], "services/supervision/roster/docs/service-roster.md"
    )
    assert re.search(
        hooks["lint-doc-roster"]["files"], "services/supervision/roster/composition.py"
    )
    for hook_id in ("lint-doc-symbols", "lint-doc-anchors"):
        assert re.search(hooks[hook_id]["files"], "docs/conventions/runbook.md")
