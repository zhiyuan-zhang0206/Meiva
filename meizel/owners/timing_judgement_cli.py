# meiva:note
# The command-line entry of the timing-judgement rule: it finds the test files, reads the baseline and
# writes the one `--write-baseline` produces, so `meizel/owners/timing_judgement.py` decides everything
# and never reads a file to learn what it governs (the shape `clock_lattice_cli.py` set).
from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

from meizel import lint_common
from meizel.graph.model import MeizelError
from meizel.owners import timing_judgement
from meizel.source_text import is_mapping, is_object_list, object_of

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


# meiva:note
# One string field of a baseline entry. A baseline this cannot read is a broken baseline, not an
# empty one: it stops here with the field's name instead of returning a shorter set that would read
# as "these sites are gone".
def _text_field(entry: Mapping[str, object], name: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str):
        raise MeizelError(f"timing baseline entry has no string {name!r}: {value!r}")
    return value


# meiva:note
# The baseline's entries, read through the untyped readers: `json.loads` is `Any`, so the document is
# narrowed to a mapping of `object`s and each entry's three strings are checked.
def _baseline_entries(path: Path) -> frozenset[tuple[str, str, str]]:
    if not path.is_file():
        return frozenset()
    document = object_of(json.loads, path.read_text(encoding="utf-8"))
    if not is_mapping(document):
        raise MeizelError(f"timing baseline {path} is not an object")
    sites = document.get("sites", ())
    if not is_object_list(sites):
        raise MeizelError(f"timing baseline {path} has no list of sites")
    keys: list[tuple[str, str, str]] = []
    for entry in sites:
        if not is_mapping(entry):
            raise MeizelError(f"timing baseline {path} has a site that is not an object")
        keys.append(
            (
                _text_field(entry, "path"),
                _text_field(entry, "function"),
                _text_field(entry, "comparison"),
            )
        )
    return frozenset(keys)


def _write(baseline_path: Path, files: list[tuple[str, str]]) -> int:
    sites = {key for rel, text in files for key in timing_judgement.site_keys(text, rel)}
    payload: dict[str, object] = {
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


def _arguments(argv: list[str]) -> tuple[list[str], bool, bool]:
    # meiva:note
    # (targets and flags left for split_only, --write-baseline, --hints).
    arguments = list(argv)
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


def main(argv: list[str], *, repo_root: Path | None = None) -> int:
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
    raise SystemExit(main(sys.argv[1:]))
