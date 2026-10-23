# meiva:note
# A test that branches on the machine it runs on injects a host, or names the one it needs.
#
# Run: `.venv/bin/python scripts/lint/host_platform_marks.py [--write-baseline] [--only FILE...] [PATH ...]`;
# also the `lint-host-platform-marks` pre-commit hook. The two rules, the exemption and the
# baseline are owned by the linked document.
# meiva:link scripts/lint/docs/host-platform-marks.meiva.okf.md

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from meizel import lint_common  # noqa: E402

BASELINE = "scripts/lint/baseline/host_platform_marks.json"

EXEMPTION = "# host-ok:"

# meiva:note
# `Host.system()` and `Platform.current()` build the host of this process; `sys.platform`,
# `os.name` and the tree's own `is_macos()` / `is_linux()` predicates are the same fact
# under other names. A predicate the tree has not written yet still reaches
# `sys.platform` somewhere below, so the family is the read, not the spelling.
_REAL_HOST_CALLS = frozenset({"Host.system", "Platform.current"})
_HOST_PREDICATES = frozenset({"is_macos", "is_linux"})
_OS_NAME = "os.name"

# meiva:note
# The stdlib `platform` module is bounded: these six calls answer "what host is this".
# `python_version()` and `python_implementation()` answer "what interpreter", which is not
# a host fact; `machine()` answers the architecture, and no marker carries that dimension.
# `getattr(platform, "system")` and `importlib.import_module("platform")` are outside the
# rule, and the document says so rather than leaving it to be discovered.
_PLATFORM_CALLS = frozenset({"system", "platform", "uname", "release", "mac_ver", "win32_ver"})

# meiva:note
# `shutil.which(name)` resolves through `os.environ["PATH"]` only when it is called without
# `path=`; an explicit path is the caller stating where to look.
_WHICH = "shutil.which"

# meiva:note
# `pytest.importorskip(name)` skips the test when the module is missing from the machine
# that runs it, which makes the lane that provisions it the one deciding the verdict.
_IMPORTORSKIP = "pytest.importorskip"


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _marked(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (_dotted(target) or "").endswith("mark.requires_host")


def _module_marked(tree: ast.Module) -> bool:
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in statement.targets):
            continue
        values = (
            statement.value.elts
            if isinstance(statement.value, ast.List | ast.Tuple)
            else [statement.value]
        )
        if any(_marked(value) for value in values):
            return True
    return False


def _platform_read(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and _dotted(node) in ("sys.platform", _OS_NAME):
        return _dotted(node)
    if isinstance(node, ast.Call):
        name = _dotted(node.func) or ""
        if name in _REAL_HOST_CALLS:
            return f"{name}()"
        predicate = name.rsplit(".", 1)[-1]
        if predicate in _HOST_PREDICATES:
            return f"{predicate}()"
        if name.startswith("platform.") and predicate in _PLATFORM_CALLS:
            return f"{name}()"
    return None


def _path_read(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    name = _dotted(node.func) or ""
    if name == _WHICH:
        if any(keyword.arg == "path" for keyword in node.keywords):
            return None
        return "shutil.which(...)"
    if name == _IMPORTORSKIP:
        return "pytest.importorskip(...)"
    return None


def _message(kind: str, what: str) -> str:
    if kind == "platform":
        return (
            f"`{what}` reads the platform of whatever process runs this test. Build the "
            "host the test wants instead (`base/host/system/tests/fake_commands.py:fake_host`), "
            "so it asserts the same result on every host; when the SUBJECT is the platform "
            '(launchd, TCC, codesign, swiftc), declare it with @pytest.mark.requires_host("darwin") '
            "and make a CI lane run it there. `# host-ok: <reason>` exempts what this rule cannot judge."
        )
    return (
        f"`{what}` resolves a program or module through the environment of whatever machine "
        "runs the test, so a developer's macOS and the CI runner disagree. Build the `Host` the "
        "test wants (`fake_host(programs={...})`) and assert through `host.which`; a lane that "
        "provisions the program instead is declared with `# host-ok: <lane>`."
    )


def _exempt(lines: list[str], span: tuple[int, int]) -> bool:
    start, end = span
    return any(EXEMPTION in line for line in lines[max(start - 1, 0) : end])


def findings_in_source(src: str) -> list[tuple[int, str, str]]:
    tree = ast.parse(src)
    lines = src.splitlines()
    out: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, *, marked: bool, span: tuple[int, int] | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(
                    child,
                    marked=marked or any(_marked(d) for d in child.decorator_list),
                    span=(child.lineno, child.end_lineno or child.lineno),
                )
                continue
            if isinstance(child, ast.expr):
                line = (child.lineno, child.end_lineno or child.lineno)
                if not marked and not _exempt(lines, span or line):
                    for kind, what in (
                        ("platform", _platform_read(child)),
                        ("path", _path_read(child)),
                    ):
                        if what is not None:
                            out.append((child.lineno, what, _message(kind, what)))
            visit(child, marked=marked, span=span)

    visit(tree, marked=_module_marked(tree), span=None)
    return out


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    text = lint_common.read_utf8_text(path)
    if text is None:
        return []
    try:
        return findings_in_source(text)
    except SyntaxError:
        return []


def _test_files(targets: list[Path]) -> Iterator[Path]:
    for target in targets:
        if target.is_file():
            if target.suffix == ".py":
                yield target
        elif target.is_dir():
            yield from target.rglob("*.py")


def _default_targets(root: Path) -> list[Path]:
    hosts = lint_common.pytest_test_hosts((root / "pyproject.toml").read_text(encoding="utf-8"))
    return [root / host for host in hosts if (root / host).is_dir()]


def _read_baseline(root: Path) -> dict[str, int]:
    path = root / BASELINE
    if not path.is_file():
        return {}
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"{BASELINE}: expected a JSON object of read -> count")
    counts: dict[str, int] = {}
    for key, value in cast("dict[str, object]", raw).items():
        if not isinstance(value, int):
            raise SystemExit(f"{BASELINE}: {key!r} must map to a count, not {value!r}")
        counts[str(key)] = value
    return counts


def _write_baseline(root: Path, judged: dict[str, list[tuple[int, str, str]]]) -> None:
    path = root / BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = dict(
        sorted(Counter(what for items in judged.values() for _, what, _ in items).items())
    )
    path.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{BASELINE}: {len(counts)} reads, {sum(counts.values())} findings")


def _judge(
    root: Path, targets: list[Path]
) -> tuple[dict[str, list[tuple[int, str, str]]], set[str]]:
    judged: dict[str, list[tuple[int, str, str]]] = {}
    scanned: set[str] = set()
    for path in sorted(lint_common.restrict(_test_files(targets), None, root)):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if not lint_common.is_test_path(rel):
            continue
        scanned.add(rel)
        findings = _scan_file(path)
        if findings:
            judged[rel] = findings
    return judged, scanned


# meiva:note
# The baseline counts how many times each read appears, wherever it appears: renaming or
# moving a test changes no count, while every new read of an already-counted kind raises
# one. A per-path baseline reddened on a rename, which is how a gate gets switched off.
def _report_over_baseline(
    judged: dict[str, list[tuple[int, str, str]]],
    baseline: dict[str, int],
    *,
    scope: frozenset[str] | None = None,
) -> bool:
    # meiva:note
    # `judged` is the whole tree even on a `--only` run: a count only means something
    # against every place the read appears. The remedy is written once per kind and the
    # sites stay one line each, because the pre-existing sites are context, not findings:
    # a site outside `scope` is marked, since a report that names a file the author never
    # opened reads as a broken gate, and a gate that looks broken is a gate people switch off.
    failed = False
    counts = Counter(what for items in judged.values() for _, what, _ in items)
    for what, count in sorted(counts.items()):
        allowed = baseline.get(what, 0)
        if count <= allowed:
            continue
        failed = True
        print(f"{what}: {count} unmarked read(s), the baseline allows {allowed}.", file=sys.stderr)
        _print_sites(judged, what, scope)
    return failed


def _print_sites(
    judged: dict[str, list[tuple[int, str, str]]], what: str, scope: frozenset[str] | None
) -> None:
    sites = [
        (rel, lineno, "" if scope is None or rel in scope else "  [outside your change]")
        for rel, items in sorted(judged.items())
        for lineno, finding, _ in items
        if finding == what
    ]
    remedy = next(
        (message for items in judged.values() for _, finding, message in items if finding == what),
        "",
    )
    print(f"  {remedy}", file=sys.stderr)
    for rel, lineno, mark in sorted(sites, key=lambda site: (bool(site[2]), site[0], site[1])):
        print(f"  {rel}:{lineno}{mark}", file=sys.stderr)


def _report_baseline_drift(
    judged: dict[str, list[tuple[int, str, str]]],
    baseline: dict[str, int],
    scope: frozenset[str] | None,
) -> bool:
    if scope is not None:
        return False
    counts = Counter(what for items in judged.values() for _, what, _ in items)
    for what, allowed in sorted(baseline.items()):
        if counts.get(what, 0) < allowed:
            print(
                f"{what}: note: {counts.get(what, 0)} unmarked read(s), baseline {allowed}; "
                "--write-baseline lowers it"
            )
    return False


def _split_args(argv: list[str]) -> tuple[list[str], list[str] | None, bool]:
    rest, only = lint_common.split_only(argv)
    write_baseline = "--write-baseline" in rest
    return [argument for argument in rest if argument != "--write-baseline"], only, write_baseline


def _arguments_problem(
    paths: list[str], only: list[str] | None, *, write_baseline: bool
) -> str | None:
    if write_baseline and (only is not None or paths):
        return "error: --write-baseline rewrites every entry; give no paths"
    missing = [argument for argument in paths if not Path(argument).exists()]
    if missing:
        return f"error: target path(s) not found: {', '.join(missing)}"
    return None


def _targets(paths: list[str], root: Path) -> list[Path]:
    return [Path(argument).resolve() for argument in paths] if paths else _default_targets(root)


# meiva:note
# The verdict: growth fails, a shrunken kind only notes. `--only` narrows what is printed,
# not what is compared — the pre-commit path has to see the whole tree's counts or a new
# read in the file under review would pass here and fail in CI.
def _verdict(
    root: Path, judged: dict[str, list[tuple[int, str, str]]], only: list[str] | None
) -> int:
    scope = lint_common.changed_scope(only, root, inputs=("pyproject.toml",))
    if scope is not None and not scope:
        return 0
    baseline = _read_baseline(root)
    over = _report_over_baseline(judged, baseline, scope=scope)
    drift = _report_baseline_drift(judged, baseline, scope)
    if not (over or drift):
        return 0
    print(
        "\nHost-platform violations. A test injects the host it wants; only a subject that IS "
        "the host (launchd, TCC, codesign, swiftc) takes @pytest.mark.requires_host. The rules "
        "are owned by scripts/lint/docs/host-platform-marks.meiva.okf.md.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    root = _REPO_ROOT if repo_root is None else repo_root
    paths, only, write_baseline = _split_args(list(sys.argv[1:] if argv is None else argv))
    problem = _arguments_problem(paths, only, write_baseline=write_baseline)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    judged = _judge(root, _targets(paths, root))[0]
    if write_baseline:
        _write_baseline(root, judged)
        return 0
    return _verdict(root, judged, only)


if __name__ == "__main__":
    sys.exit(main())
