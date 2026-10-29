# meiva:note
# A test that branches on the machine it runs on injects a host, or names the one it needs.
#
# Run: `.venv/bin/python scripts/lint/host_platform_marks.py [--write-baseline] [--only FILE...] [PATH ...]`;
# also the `lint-host-platform-marks` pre-commit hook. The two rules, the exemption and the
# baseline are owned by the linked document.
# meiva:link scripts/lint/docs/host-platform-marks.meiva.okf.md

from __future__ import annotations

import ast
import io
import json
import sys
import tokenize
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeGuard

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from meizel import lint_common  # noqa: E402
from meizel.roles import packages  # noqa: E402

BASELINE = "scripts/lint/baseline/host_platform_marks.json"

# meiva:note
# The exemptions are a record of their own: `<path>::<owner>` -> the reason its reader
# sees. A comment alone was a free pass — nobody reviews a line they do not expect — so an
# exemption now has to exist here, with a reason that names a path in this repository or a
# CI job, before the lint will honour it. `allowed` is the frozen count.
EXEMPTIONS = "scripts/lint/baseline/host_platform_exemptions.json"

EXEMPTION = "# host-ok:"

# meiva:note
# The top-level directories a grounding path can start with: the packages of this
# repository, read from the role the manifests own, plus the two trees that are not
# packages. A reason that points at one of them tells the next reader where to look.
_REPO_TOPS = frozenset(
    {*packages("application", "standalone"), *packages("tooling"), "docs", "tests", "future"}
)

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
    return _findings_in_tree(ast.parse(src), src)


def _findings_in_tree(tree: ast.Module, src: str) -> list[tuple[int, str, str]]:
    lines = src.splitlines()
    # meiva:note
    # `_exempt` looks for the marker in the lines of a span, so a file that never writes it
    # has no exemption anywhere: skipping the search keeps the verdict and drops a scan of
    # every expression's lines (2026-10-29: it was the largest single cost of a whole-tree run).
    exempt_anywhere = EXEMPTION in src
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
                if not marked and not (exempt_anywhere and _exempt(lines, span or line)):
                    for kind, what in (
                        ("platform", _platform_read(child)),
                        ("path", _path_read(child)),
                    ):
                        if what is not None:
                            out.append((child.lineno, what, _message(kind, what)))
            visit(child, marked=marked, span=span)

    visit(tree, marked=_module_marked(tree), span=None)
    return out


# meiva:note
# Every `# host-ok:` comment in `src` with the function that owns it (`<module>` at module
# level) and the reason as written. Read through the tokenizer, not the lines: a fixture
# that writes the marker into a string literal is data, not an exemption. The innermost
# function wins — an exemption inside a method does not exempt the class.
def exemptions_in_source(src: str) -> list[tuple[str, str]]:
    return _exemptions_in_tree(ast.parse(src), src)


def _exemptions_in_tree(tree: ast.Module, src: str) -> list[tuple[str, str]]:
    # meiva:note
    # An exemption is a comment that contains the marker, and a comment token's text is a
    # substring of the source: without the marker in the source there is nothing to read,
    # so the tokenizer pass and the owner walk are both skipped.
    if EXEMPTION not in src:
        return []
    owners = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    out: list[tuple[str, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(src).readline):
        if token.type != tokenize.COMMENT or EXEMPTION not in token.string:
            continue
        reason = token.string.split(EXEMPTION, 1)[1].strip()
        inside = [(start, name) for start, end, name in owners if start <= token.start[0] <= end]
        out.append((max(inside, default=(0, "<module>"))[1], reason))
    return out


# meiva:note
# A reason earns an exemption by saying where to look: a path in this repository
# (`base/host/...`, `docs/conventions/...`) or a CI job that runs the test there.
def reason_is_grounded(reason: str, jobs: frozenset[str]) -> bool:
    for token in reason.replace("`", " ").replace(",", " ").split():
        parts = token.strip(".:;()[]").split("/")
        if len(parts) > 1 and parts[0] in _REPO_TOPS:
            return True
    return any(job and job in reason for job in jobs)


# meiva:note
# The CI job ids and names an exemption may cite, read from the workflow that runs them.
def job_names(root: Path) -> frozenset[str]:
    path = root / ".github/workflows/ci.yml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    names = {
        line.split(":")[0].strip()
        for line in text.splitlines()
        if line.startswith("  ") and line.rstrip().endswith(":")
    }
    names |= {
        line.split("name:")[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("name:")
    }
    return frozenset(name.strip("\"'") for name in names if name.strip())


_Parse = Callable[[str], ast.Module]


# meiva:note
# Both verdicts come from one parse: the findings and the exemptions read the same tree,
# and a whole-tree run parses every test file it walks (2026-10-29: 3192 files, 21.4 MB).
# The parser is a parameter so a test can count the parses instead of patching `ast`
# (`docs/conventions/python-conventions.md`: a test takes what it needs to control).
# The exemption pass keeps its own guard: a source that parses can still fail to lex, and
# that used to cost the exemptions alone, not the findings.
def judge_source(
    text: str, *, parse: _Parse = ast.parse
) -> tuple[list[tuple[int, str, str]], list[tuple[str, str]]]:
    try:
        tree = parse(text)
    except SyntaxError:
        return [], []
    findings = _findings_in_tree(tree, text)
    try:
        exemptions = _exemptions_in_tree(tree, text)
    except SyntaxError:
        return findings, []
    return findings, exemptions


def _test_files(targets: list[Path], root: Path) -> Iterator[Path]:
    yield from lint_common.python_files(targets, root)


def _default_targets(root: Path) -> list[Path]:
    hosts = lint_common.pytest_test_hosts((root / "pyproject.toml").read_text(encoding="utf-8"))
    return [root / host for host in (*hosts, *packages("standalone")) if (root / host).is_dir()]


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _read_baseline(root: Path) -> dict[str, int]:
    path = root / BASELINE
    if not path.is_file():
        return {}
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not _is_str_dict(raw):
        raise SystemExit(f"{BASELINE}: expected a JSON object of read -> count")
    counts: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(value, int):
            raise SystemExit(f"{BASELINE}: {key!r} must map to a count, not {value!r}")
        counts[key] = value
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
) -> tuple[dict[str, list[tuple[int, str, str]]], dict[str, list[str]]]:
    judged: dict[str, list[tuple[int, str, str]]] = {}
    exemptions: dict[str, list[str]] = {}
    scanned: set[str] = set()
    for path in sorted(lint_common.restrict(_test_files(targets, root), None, root)):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if not lint_common.is_test_path(rel):
            continue
        scanned.add(rel)
        findings, found_exemptions = judge_source(lint_common.read_utf8_text(path) or "")
        if findings:
            judged[rel] = findings
        for owner, reason in found_exemptions:
            exemptions.setdefault(f"{rel}::{owner}", []).append(reason)
    return judged, exemptions


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


def _read_exemptions(root: Path) -> tuple[int, dict[str, str]]:
    path = root / EXEMPTIONS
    if not path.is_file():
        return 0, {}
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not _is_str_dict(raw):
        raise SystemExit(f"{EXEMPTIONS}: expected {{'allowed': int, 'exemptions': {{...}}}}")
    allowed = raw.get("allowed")
    entries = raw.get("exemptions")
    if not isinstance(allowed, int) or not _is_str_dict(entries):
        raise SystemExit(f"{EXEMPTIONS}: expected {{'allowed': int, 'exemptions': {{...}}}}")
    return allowed, {key: str(value) for key, value in entries.items()}


def _write_exemptions(root: Path, found: dict[str, list[str]], jobs: frozenset[str]) -> None:
    path = root / EXEMPTIONS
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = {key: reasons[0] for key, reasons in sorted(found.items())}
    ungrounded = [key for key, reason in entries.items() if not reason_is_grounded(reason, jobs)]
    if ungrounded:
        raise SystemExit(f"{EXEMPTIONS}: reason names no path or job for {', '.join(ungrounded)}")
    payload = {"allowed": len(entries), "exemptions": entries}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{EXEMPTIONS}: {len(entries)} exemption(s)")


# meiva:note
# An exemption is a reviewed exception, not a comment: it has to be in the record, its
# reason has to be the one on disk, and that reason has to say where to look. A record
# entry whose site is gone is an error too - the count only shrinks when someone spends
# the line to shrink it.
def _report_exemptions(
    found: dict[str, list[str]],
    *,
    root: Path,
    jobs: frozenset[str],
    scope: frozenset[str] | None,
) -> bool:
    if scope is not None:
        return False
    allowed, record = _read_exemptions(root)
    failed = False
    if len(record) != allowed:
        failed = True
        print(
            f"{EXEMPTIONS}: allowed says {allowed}, the record lists {len(record)}",
            file=sys.stderr,
        )
    for key, reasons in sorted(found.items()):
        reason = reasons[0]
        if key not in record:
            failed = True
            print(
                f"{key}: unrecorded exemption {reason!r} - add it to {EXEMPTIONS} with a "
                "reason that names a path in this repository or a CI job",
                file=sys.stderr,
            )
        elif record[key] != reason:
            failed = True
            print(
                f"{key}: the record's reason differs from the comment - {EXEMPTIONS} says "
                f"{record[key]!r}, the tree says {reason!r}",
                file=sys.stderr,
            )
        elif not reason_is_grounded(reason, jobs):
            failed = True
            print(
                f"{key}: reason names no path or job: {reason!r} - say where to look "
                f"(`base/...`, `docs/...`) or which CI job runs it",
                file=sys.stderr,
            )
    for key in sorted(set(record) - set(found)):
        failed = True
        print(
            f"{key}: recorded exemption is no longer in the tree - `--write-baseline` drops it",
            file=sys.stderr,
        )
    return failed


def _report_baseline_drift(
    judged: dict[str, list[tuple[int, str, str]]],
    baseline: dict[str, int],
    scope: frozenset[str] | None,
) -> bool:
    if scope is not None:
        return False
    counts = Counter(what for items in judged.values() for _, what, _ in items)
    drifted = False
    for what, allowed in sorted(baseline.items()):
        actual = counts.get(what, 0)
        if actual < allowed:
            print(
                f"{what}: {actual} unmarked read(s) below the recorded {allowed} - this change "
                "removed a read the record still counts; lower it with "
                "`.venv/bin/python scripts/lint/host_platform_marks.py --write-baseline`",
                file=sys.stderr,
            )
            drifted = True
    return drifted


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
# The verdict: both directions are judged. Growth fails because the tree now reads the host
# where it used to inject one; shrinkage fails because the record and the tree are the same
# fact, and a record that outlives its reads makes the next author's reduction invisible
# (2026-10-24: two merged PRs each removed a read and left the count, and every later branch
# carrying this record failed the merge-tree run instead). `--only` narrows what is printed,
# not what is compared — the pre-commit path has to see the whole tree's counts or a new read
# in the file under review would pass here and fail in CI.
def _verdict(
    root: Path,
    judged: dict[str, list[tuple[int, str, str]]],
    exemptions: dict[str, list[str]],
    only: list[str] | None,
) -> int:
    scope = lint_common.changed_scope(only, root, inputs=("pyproject.toml",))
    if scope is not None and not scope:
        return 0
    baseline = _read_baseline(root)
    over = _report_over_baseline(judged, baseline, scope=scope)
    drift = _report_baseline_drift(judged, baseline, scope)
    exemptions_bad = _report_exemptions(exemptions, root=root, jobs=job_names(root), scope=scope)
    if not (over or drift or exemptions_bad):
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
    judged, exemptions = _judge(root, _targets(paths, root))
    if write_baseline:
        _write_baseline(root, judged)
        _write_exemptions(root, exemptions, job_names(root))
        return 0
    return _verdict(root, judged, exemptions, only)


if __name__ == "__main__":
    sys.exit(main())
