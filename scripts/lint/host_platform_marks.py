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
from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from meizel import lint_common  # noqa: E402

BASELINE = "scripts/lint/baseline/host_platform_marks.json"

EXEMPTION = "# host-ok:"

# meiva:note
# `Host.system()` and `Platform.current()` build the host of this process; `sys.platform` is
# the same fact under another name.
_REAL_HOST_CALLS = frozenset({"Host.system", "Platform.current"})

# meiva:note
# `shutil.which(name)` resolves through `os.environ["PATH"]` only when it is called without
# `path=`; an explicit path is the caller stating where to look.
_WHICH = "shutil.which"


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
    if isinstance(node, ast.Attribute) and _dotted(node) == "sys.platform":
        return "sys.platform"
    if isinstance(node, ast.Call):
        name = _dotted(node.func)
        if name in _REAL_HOST_CALLS:
            return f"{name}()"
    return None


def _path_read(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or _dotted(node.func) != _WHICH:
        return None
    if any(keyword.arg == "path" for keyword in node.keywords):
        return None
    return "shutil.which(...)"


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
        f"`{what}` resolves a program through the PATH of whatever machine runs the test, so a "
        "developer's macOS and the CI runner disagree. Build the `Host` the test wants "
        "(`fake_host(programs={...})`) and assert through `host.which`; a lane that provisions "
        "the program instead is declared with `# host-ok: <lane>`."
    )


def _exempt(lines: list[str], span: tuple[int, int]) -> bool:
    start, end = span
    return any(EXEMPTION in line for line in lines[max(start - 1, 0) : end])


def findings_in_source(src: str) -> list[tuple[int, str]]:
    tree = ast.parse(src)
    lines = src.splitlines()
    out: list[tuple[int, str]] = []

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
                            out.append((child.lineno, _message(kind, what)))
            visit(child, marked=marked, span=span)

    visit(tree, marked=_module_marked(tree), span=None)
    return out


def _scan_file(path: Path) -> list[tuple[int, str]]:
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
        raise SystemExit(f"{BASELINE}: expected a JSON object of path -> count")
    counts: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, int):
            raise SystemExit(f"{BASELINE}: {key!r} must map to a count, not {value!r}")
        counts[key] = value
    return counts


def _write_baseline(root: Path, found: dict[str, list[tuple[int, str]]]) -> None:
    path = root / BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {rel: len(items) for rel, items in sorted(found.items()) if items}
    path.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{BASELINE}: {len(counts)} files, {sum(counts.values())} findings")


def _judge(
    root: Path, targets: list[Path], scope: frozenset[str] | None
) -> tuple[dict[str, list[tuple[int, str]]], set[str]]:
    judged: dict[str, list[tuple[int, str]]] = {}
    scanned: set[str] = set()
    for path in sorted(lint_common.restrict(_test_files(targets), scope, root)):
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


def _report_over_baseline(
    judged: dict[str, list[tuple[int, str]]], baseline: dict[str, int]
) -> bool:
    failed = False
    for rel, items in sorted(judged.items()):
        allowed = baseline.get(rel, 0)
        if len(items) <= allowed:
            continue
        failed = True
        print(
            f"{rel}: error: {len(items)} unmarked host read(s), the baseline allows "
            f"{allowed}. The findings:",
            file=sys.stderr,
        )
        for lineno, message in items:
            print(f"  {rel}:{lineno}: error: {message}", file=sys.stderr)
    return failed


def _report_baseline_drift(
    judged: dict[str, list[tuple[int, str]]],
    scanned: set[str],
    baseline: dict[str, int],
    scope: frozenset[str] | None,
) -> bool:
    if scope is not None:
        return False
    failed = False
    for rel in sorted(set(baseline) - scanned):
        failed = True
        print(
            f"{rel}: error: this file has a baseline entry but no longer exists; "
            "run --write-baseline to drop it",
            file=sys.stderr,
        )
    for rel, items in sorted(judged.items()):
        allowed = baseline.get(rel, 0)
        if len(items) < allowed:
            print(
                f"{rel}: note: {len(items)} unmarked host read(s), baseline {allowed}; "
                "--write-baseline lowers it"
            )
    return failed


def _split_args(argv: list[str]) -> tuple[list[str], list[str] | None, bool]:
    rest, only = lint_common.split_only(argv)
    write_baseline = "--write-baseline" in rest
    return [argument for argument in rest if argument != "--write-baseline"], only, write_baseline


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    root = _REPO_ROOT if repo_root is None else repo_root
    paths, only, write_baseline = _split_args(list(sys.argv[1:] if argv is None else argv))
    if write_baseline and (only is not None or paths):
        print("error: --write-baseline rewrites every entry; give no paths", file=sys.stderr)
        return 1
    missing = [argument for argument in paths if not Path(argument).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    targets = [Path(argument).resolve() for argument in paths] if paths else _default_targets(root)
    scope = lint_common.changed_scope(only, root, inputs=("pyproject.toml",))
    judged, scanned = _judge(root, targets, scope)
    if write_baseline:
        _write_baseline(root, judged)
        return 0
    baseline = _read_baseline(root)
    over = _report_over_baseline(judged, baseline)
    drift = _report_baseline_drift(judged, scanned, baseline, scope)
    if not (over or drift):
        return 0
    print(
        "\nHost-platform violations. A test injects the host it wants; only a subject that IS "
        "the host (launchd, TCC, codesign, swiftc) takes @pytest.mark.requires_host. The rules "
        "are owned by scripts/lint/docs/host-platform-marks.meiva.okf.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
