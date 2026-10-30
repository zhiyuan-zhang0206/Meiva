# meiva:note
# The command-line entry of the timing-judgement rule: it finds the test files, reads the baseline and
# writes the one `--write-baseline` produces, so `meizel/owners/timing_judgement.py` decides everything
# and never reads a file to learn what it governs (the shape `clock_lattice_cli.py` set).
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from meizel import lint_common
from meizel.owners import timing_judgement

_REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path("meizel/owners/timing_judgement_baseline.json")


def _targets(argv: list[str], *, root: Path) -> list[Path]:
    if argv:
        return [Path(argument).resolve() for argument in argv]
    hosts = lint_common.pytest_test_hosts((root / "pyproject.toml").read_text(encoding="utf-8"))
    return [root / host for host in hosts if (root / host).is_dir()]


def _python_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def _test_files(paths: list[Path], root: Path) -> list[tuple[str, str]]:
    # meiva:note
    # (relative path, text) for the readable test files among `paths`.
    pairs: list[tuple[str, str]] = []
    for path in paths:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        text = lint_common.read_utf8_text(path)
        if text is not None and lint_common.is_test_path(rel):
            pairs.append((rel, text))
    return pairs


def _baseline_entries(path: Path) -> frozenset[tuple[str, str, str]]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return frozenset((s["path"], s["function"], s["comparison"]) for s in data.get("sites", ()))


def _write(baseline_path: Path, files: list[tuple[str, str]]) -> int:
    sites = {key for rel, text in files for key in timing_judgement.site_keys(text, rel)}
    payload: dict[str, Any] = {
        "note": (
            "Timing judgements that predate the rule (the #5821 inventory, re-scanned). "
            "Regenerate with --write-baseline and treat the diff as the review."
        ),
        "sites": [
            {"path": path, "function": function, "comparison": comparison}
            for path, function, comparison in sorted(sites)
        ],
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"timing-guard: {len(sites)} baseline site(s) written to {BASELINE}")
    return 0


def _arguments(argv: list[str] | None) -> tuple[list[str], bool, bool]:
    # meiva:note
    # (targets and flags left for split_only, --write-baseline, --hints).
    arguments = list(sys.argv[1:] if argv is None else argv)
    write_baseline = "--write-baseline" in arguments
    hints = "--hints" in arguments
    return [a for a in arguments if a not in ("--write-baseline", "--hints")], write_baseline, hints


def _judge(files: list[tuple[str, str]], root: Path, only: list[str] | None, *, hints: bool) -> int:
    scope = lint_common.changed_scope(only, root, inputs=(BASELINE.as_posix(),))
    found = timing_judgement.findings(
        files, baseline=_baseline_entries(root / BASELINE), scope=scope
    )
    for rel, line, message in found:
        print(f"{rel}:{line}: error: {message}")
    if hints:
        judged = [text for rel, text in files if scope is None or rel in scope]
        print(
            f"timing-guard: {sum(t.count('wait_for(') for t in judged)} `asyncio.wait_for(` call site(s) are not judged."
        )
    if not found:
        return 0
    print(
        f"\n{len(found)} timing judgement(s). See the docstring at the top of "
        "meizel/owners/timing_judgement.py: assert a counted fact, or state why a clock is the "
        "only judge left.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    root = _REPO_ROOT if repo_root is None else repo_root
    arguments, write_baseline, hints = _arguments(argv)
    arguments, only = lint_common.split_only(arguments)
    missing = [argument for argument in arguments if not Path(argument).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    files = _test_files(_python_files(_targets(arguments, root=root)), root)
    if write_baseline:
        return _write(root / BASELINE, files)
    return _judge(files, root, only, hints=hints)


if __name__ == "__main__":
    raise SystemExit(main())
