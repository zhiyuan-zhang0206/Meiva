# meiva:note
# The shell of the diff hints: it reads git, builds the values the engine takes, and
# prints what the renderer returns. Three commands.
#
#   hints.py commit-msg MSGFILE     the hook of the commit-msg stage: the staged diff must be
#                                   answered by the `Hint-Ack` trailers of this message.
#   hints.py branch                 the hook of the pre-push stage: the diff of merge-base
#                                   (origin/main, HEAD)..HEAD must be answered by the
#                                   trailers of any commit on the branch.
#   hints.py report BASE HEAD       every site of a diff, answered or not, for calibration.
#   hints.py stats [BASE HEAD]      the counted shapes (stats.py) of a diff or, with --tree, of the tree.
#
# The rule, the catalog and the trailer format are in
# scripts/lint/docs/diff-hints/diff-hints.meiva.okf.md.
# meiva:link scripts/lint/docs/diff-hints/diff-hints.meiva.okf.md
from __future__ import annotations

import argparse
import ast
import collections
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypeGuard

# meiva:note
# A standalone script: the repository root goes on the path before the first import of
# the repository's own packages.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT))

from meizel.graph.model import AmbientKind  # noqa: E402
from meizel.roles import all_packages, packages  # noqa: E402
from scripts.lint.diagnostics.diff_hints.acks import (  # noqa: E402
    answers,
    parse_acks,
    unanswered,
    unused,
)
from scripts.lint.diagnostics.diff_hints.diff import parse_diff  # noqa: E402
from scripts.lint.diagnostics.diff_hints.engine import analyze  # noqa: E402
from scripts.lint.diagnostics.diff_hints.model import (  # noqa: E402
    MIN_IMPORTERS,
    Ack,
    FileChange,
    FileKind,
    Importers,
    NewRoot,
    Promotion,
    Roles,
    Site,
    StatId,
    StatSite,
)
from scripts.lint.diagnostics.diff_hints.promotion import strict_promotion  # noqa: E402
from scripts.lint.diagnostics.diff_hints.render import (  # noqa: E402
    render_errors,
    render_open,
    render_summary,
)
from scripts.lint.diagnostics.diff_hints.scope import classify, is_test_path  # noqa: E402
from scripts.lint.diagnostics.diff_hints.stats import statistics  # noqa: E402

# meiva:note
# The diff the hints read. Prefixes are explicit because `diff.mnemonicPrefix` and
# `diff.noprefix` rewrite them, and a path that keeps a prefix is out of every scope, so the
# hook would pass silently. `-M` makes a pure rename a header without hunks (nothing added,
# nothing asked) and a rename with edits a diff of the edits only.
_DIFF = (
    "diff",
    "-U0",
    "--no-color",
    "--no-ext-diff",
    "-M",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--diff-filter=ACMRTD",
)


class GitError(RuntimeError):
    pass


# meiva:note
# Fixed argument vectors only: git from PATH, never a shell, never user text as a command.
def _git(*args: str) -> bytes:
    done = subprocess.run(  # noqa: S603
        ["git", "-c", "core.quotePath=false", "-c", "diff.renameLimit=5000", *args],
        capture_output=True,
        check=False,
    )
    if done.returncode:
        message = done.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {message}")
    return done.stdout


def _roles() -> Roles:
    return Roles(
        production=frozenset(packages("application", "templates")),
        tests=frozenset(packages("tests")),
        first_party=frozenset(all_packages()),
    )


# meiva:note
# The text of each path at `rev` (the index when `rev` is empty), by one `git cat-file
# --batch`. A path git cannot find is left out of the result.
def _read_blobs(rev: str, paths: Sequence[str]) -> dict[str, str]:
    if not paths:
        return {}
    request = "".join(f"{rev}:{path}\n" for path in paths).encode()
    done = subprocess.run(
        ["git", "cat-file", "--batch"], input=request, capture_output=True, check=False
    )
    if done.returncode:
        raise GitError(f"git cat-file --batch failed: {done.stderr.decode('utf-8', 'replace')}")
    out, at = done.stdout, 0
    texts: dict[str, str] = {}
    for path in paths:
        end = out.index(b"\n", at)
        header = out[at:end].split()
        at = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            texts[path] = out[at : at + size].decode("utf-8", "replace")
            at += size + 1
    return texts


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# The `[roots]` table of one manifest text: each declared file and its kinds. The manifest checker
# validates the full declaration; the hint rejects a malformed table or kind list.
def _declared(manifest: str, text: str) -> dict[str, frozenset[AmbientKind]]:
    raw: object = tomllib.loads(text).get("roots", {})
    if not _is_str_dict(raw):
        raise GitError(f"{manifest}: [roots] must be a table")
    found: dict[str, frozenset[AmbientKind]] = {}
    for name, value in raw.items():
        if not _is_str_dict(value):
            raise GitError(f"{manifest}: [roots] {name!r} must be a table with kinds and why")
        listed = value.get("kinds")
        if not _is_object_list(listed):
            raise GitError(f"{manifest}: [roots] {name!r} must declare kinds")
        kinds: set[AmbientKind] = set()
        for item in listed:
            if not isinstance(item, str):
                raise GitError(f"{manifest}: root kinds must be strings")
            kinds.add(AmbientKind(item))
        found[name] = frozenset(kinds)
    return found


# meiva:note
# Read roots from the same Git revision as the Python, including the index at commit-msg.
def _root_kinds(rev: str, paths: Sequence[str]) -> dict[str, frozenset[AmbientKind]]:
    manifests = {path: str(Path(path).parent / "meizel.toml") for path in paths}
    texts = _read_blobs(rev, sorted(set(manifests.values())))
    found: dict[str, frozenset[AmbientKind]] = {}
    for path, manifest in manifests.items():
        text = texts.get(manifest)
        if text is not None:
            found[path] = _declared(manifest, text).get(Path(path).name, frozenset())
    return found


# meiva:note
# The tracked Python files at `rev` (the index when it is empty) that contain one of the fixed
# strings. `git grep` exits 1 for no match, which is an answer and not a failure.
def _grep(rev: str, needles: Sequence[str], pathspec: str, *, every: bool = False) -> set[str]:
    where = ["--cached"] if not rev else [rev]
    flags = ["--all-match"] if every else []
    command = ["git", "grep", "-l", "-F", *flags, *(a for n in needles for a in ("-e", n)), *where]
    done = subprocess.run([*command, "--", pathspec], capture_output=True, check=False)  # noqa: S603
    if done.returncode not in (0, 1):
        raise GitError(f"git grep failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    prefix = f"{rev}:" if rev else ""
    return {
        line.removeprefix(prefix) for line in done.stdout.decode("utf-8", "replace").splitlines()
    }


# meiva:note
# The dotted modules a file imports, from its syntax tree: `import a.b.c` imports `a`, `a.b` and
# `a.b.c`; `from a.b import c` imports `a.b` and, since `c` may be a module, `a.b.c`; a relative
# import is resolved against the importing file's package. Text that only looks like an import
# (a string, a comment) is not an import.
def _imported_modules(path: str, text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    package = path.split("/")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                found.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
        elif isinstance(node, ast.ImportFrom):
            if node.level > len(package) + 1:
                continue
            base = package[: len(package) - (node.level - 1)] if node.level else []
            parts = [*base, *(node.module.split(".") if node.module else [])]
            found.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
            found.update(f"{'.'.join(parts)}.{alias.name}" for alias in node.names if parts)
    return found


# meiva:note
# The files that import `path` as a module. The candidates are the tracked files that mention the
# dotted name or the `from <package> import` line, and the files of the same top-level package that
# have a relative import and mention the module's last name; their syntax decides.
def _importing_files(rev: str, path: str) -> set[str]:
    module = path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
    parent, _, leaf = module.rpartition(".")
    candidates = _grep(rev, [module, *([f"from {parent} import"] if parent else [])], "*.py")
    top = path.split("/", 1)[0]
    if top != path:
        candidates |= _grep(rev, ["from .", leaf], f"{top}/*.py", every=True)
    candidates.discard(path)
    texts = _read_blobs(rev, sorted(candidates))
    return {found for found, text in texts.items() if module in _imported_modules(found, text)}


# meiva:note
# The production modules that import `path` and are not declared roots themselves.
def _plain_importers(rev: str, path: str, roles: Roles) -> tuple[str, ...]:
    importers = sorted(
        p for p in _importing_files(rev, path) if classify(p, roles) is FileKind.PRODUCTION
    )
    also_roots = _root_kinds(rev, importers)
    return tuple(p for p in importers if not also_roots.get(p))


# meiva:note
# For a declared root: its importers that are not roots, and, when it has fewer than the threshold,
# how many modules import the one it has.
def _importers_of(rev: str, path: str, roles: Roles) -> Importers:
    direct = _plain_importers(rev, path, roles)
    onward = ()
    if 0 < len(direct) < MIN_IMPORTERS:
        onward = tuple((p, len(_plain_importers(rev, p, roles))) for p in direct)
    return Importers(direct, onward)


def _importer_counts(
    rev: str, roots: Mapping[str, frozenset[AmbientKind]], roles: Roles
) -> dict[str, Importers]:
    return {path: _importers_of(rev, path, roles) for path, kinds in roots.items() if kinds}


_TYPESCRIPT = (".ts", ".tsx", ".mts", ".cts")


# meiva:note
# The roots a change declares in a manifest: the files named in a `[roots]` table now and not in the
# same manifest at the base. A production Python file and a TypeScript source (not a test) are asked
# about; their importers are read for Python only.
def _new_roots(spec: Sequence[str], rev: str, roles: Roles) -> list[NewRoot]:
    names = _git(
        *_DIFF[:1], "--name-only", "--no-color", "--diff-filter=ACMR", *spec, "--", "*meizel.toml"
    )
    manifests = [
        m for m in names.decode("utf-8", "replace").splitlines() if Path(m).name == "meizel.toml"
    ]
    now = _read_blobs(rev, manifests)
    before = _read_blobs(spec[0] if len(spec) == 2 else "HEAD", manifests)
    found: list[NewRoot] = []
    for manifest in manifests:
        after = _declared(manifest, now[manifest]) if manifest in now else {}
        old = _declared(manifest, before[manifest]) if manifest in before else {}
        for name, kinds in sorted(after.items()):
            path = (Path(manifest).parent / name).as_posix()
            python = classify(path, roles) is FileKind.PRODUCTION
            if name in old or not (
                python or (path.endswith(_TYPESCRIPT) and not is_test_path(path))
            ):
                continue
            found.append(
                NewRoot(path, manifest, kinds, _importers_of(rev, path, roles) if python else None)
            )
    return found


# meiva:note
# The levels a manifest text gives its children: `strict`, `strict-to-be` or `none` by file, with `"."`
# for the directory. A malformed table is the manifest checker's finding, not this hint's.
def _purity_levels(text: str) -> dict[str, str]:
    raw: object = tomllib.loads(text).get("purity")
    if isinstance(raw, str):
        return {".": raw}
    if not _is_str_dict(raw):
        return {}
    levels: dict[str, str] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            levels[name] = value
        elif _is_str_dict(value) and isinstance(value.get("mode"), str):
            levels[name] = str(value["mode"])
    return levels


# meiva:note
# The files the change promotes to `strict` in a manifest: strict at the head and not at the base,
# where a file the manifest does not name has the level of its directory.
def _promotions(spec: Sequence[str], rev: str) -> list[Promotion]:
    names = _git(
        *_DIFF[:1], "--name-only", "--no-color", "--diff-filter=ACMR", *spec, "--", "*meizel.toml"
    )
    manifests = [
        m for m in names.decode("utf-8", "replace").splitlines() if Path(m).name == "meizel.toml"
    ]
    now = _read_blobs(rev, manifests)
    before = _read_blobs(spec[0] if len(spec) == 2 else "HEAD", manifests)
    found: list[Promotion] = []
    for manifest in manifests:
        after = _purity_levels(now[manifest]) if manifest in now else {}
        old = _purity_levels(before[manifest]) if manifest in before else {}
        for name, level in sorted(after.items()):
            was = old.get(name, old.get(".", "none"))
            if level == "strict" and was != "strict" and name.endswith(".py"):
                path = (Path(manifest).parent / name).as_posix()
                found.append(Promotion(path, manifest, was))
    return found


def _diff(
    spec: Sequence[str], rev: str, roles: Roles
) -> tuple[tuple[FileChange, ...], dict[str, str]]:
    patch = _git(*_DIFF, *spec, "--", "*.py").decode("utf-8", "replace")
    changes = parse_diff(patch)
    live = [
        c.path
        for c in changes
        if c.added and not c.deleted and classify(c.path, roles) is not FileKind.OTHER
    ]
    return changes, _read_blobs(rev, live)


def _sites(spec: Sequence[str], rev: str) -> list[Site]:
    roles = _roles()
    changes, texts = _diff(spec, rev, roles)
    roots = _root_kinds(rev, tuple(texts))
    found = analyze(
        changes,
        texts,
        roles,
        roots,
        _importer_counts(rev, roots, roles),
        _new_roots(spec, rev, roles),
    )
    found.extend(strict_promotion(_promotions(spec, rev), changes))
    return sorted(found, key=lambda site: (site.path, site.line, site.hint.value))


# meiva:note
# The `optional-test-only` candidates that only tests pass: a keyword `name=` that no production
# module spells, apart from the parameter's own line. A statistic, so a spelling inside a string or
# another call of the same name counts as a caller, and the count errs towards fewer sites.
def _only_tests_pass(sites: list[StatSite], rev: str, roles: Roles) -> list[StatSite]:
    kept: list[StatSite] = []
    for site in sites:
        if site.stat is not StatId.OPTIONAL_TEST_ONLY:
            kept.append(site)
            continue
        function, _, name = site.detail.partition(":")
        spelled = re.compile(rf"(?<![\w.]){re.escape(name)}=(?!=)")
        texts = _read_blobs(rev, sorted(_grep(rev, [f"{name}="], "*.py")))
        callers = [
            path
            for path, text in texts.items()
            if any(
                path != site.path or text.count("\n", 0, found.start()) + 1 != site.line
                for found in spelled.finditer(text)
            )
        ]
        production = [p for p in callers if classify(p, roles) is FileKind.PRODUCTION]
        tests = [p for p in callers if classify(p, roles) is FileKind.TEST]
        if tests and not production:
            detail = (
                f"`{name}` of `{function}` is passed by keyword in {len(tests)} test file(s), "
                "in no production module"
            )
            kept.append(StatSite(site.stat, site.path, site.line, detail))
    return kept


def _tree_changes(head: str, roles: Roles) -> tuple[tuple[FileChange, ...], dict[str, str]]:
    listed = _git("ls-tree", "-r", "--name-only", head).decode("utf-8", "replace").splitlines()
    paths = [p for p in listed if classify(p, roles) is FileKind.PRODUCTION]
    texts = _read_blobs(head, paths)
    changes = tuple(
        FileChange(path, frozenset(range(1, text.count("\n") + 2)), (), (), deleted=False)
        for path, text in texts.items()
    )
    return changes, texts


# meiva:note
# The statistics of a diff (`BASE HEAD`) or, with `--tree`, of every production file at HEAD: how many
# sites of each shape, in how many files, the files with the most, and the first sites. Nothing is
# asked and the exit code is 0.
def _stats(base: str, head: str, *, tree: bool, shown: int) -> int:
    roles = _roles()
    if tree:
        changes, texts = _tree_changes(head, roles)
    else:
        changes, texts = _diff([base, head], head, roles)
    sites = _only_tests_pass(statistics(changes, texts, roles), head, roles)
    for stat in StatId:
        found = [site for site in sites if site.stat is stat]
        per_file = collections.Counter(site.path for site in found)
        sys.stdout.write(f"{stat.value}: {len(found)} sites in {len(per_file)} files\n")
        for path, count in per_file.most_common(10):
            sys.stdout.write(f"  {count:3d}  {path}\n")
        for site in found[:shown]:
            sys.stdout.write(f"{site.path}:{site.line}: {stat.value}: {site.detail}\n")
    return 0


# meiva:note
# The fork point of `head` from `base_ref`. A missing ref fails explicitly: not knowing the
# base is not a reason to claim an empty contribution.
def _merge_base(base_ref: str, head: str) -> str:
    try:
        _git("rev-parse", "--verify", "-q", base_ref)
    except GitError as error:
        raise GitError(f"cannot find {base_ref}; fetch it before pushing") from error
    return _git("merge-base", base_ref, head).decode().strip()


def _commits(base: str, head: str) -> list[tuple[str, str]]:
    raw = _git("log", "--format=%H%x1f%B%x00", f"{base}..{head}").decode("utf-8", "replace")
    entries = [entry.strip("\n") for entry in raw.split("\0") if entry.strip()]
    return [(sha[:9], message) for sha, _, message in (e.partition("\x1f") for e in entries)]


def _report_problems(errors: tuple[str, ...], notes: tuple[str, ...]) -> None:
    for note in notes:
        sys.stderr.write(f"diff hints: note: {note}\n")
    if errors:
        sys.stderr.write(render_errors(errors) + "\n")


def _verdict(sites: list[Site], acks: tuple[Ack, ...], *, branch: bool) -> list[Site]:
    open_sites = unanswered(sites, acks, ignore_line=branch)
    for ack in [] if branch else unused(acks, sites):
        message = f"note: the answer `{ack.hint.value} {ack.path}` matches no open site"
        sys.stderr.write(f"diff hints: {message}\n")
    if open_sites:
        sys.stderr.write(render_open(open_sites) + "\n")
    return open_sites


def _commit_msg(message_file: str) -> int:
    message = Path(message_file).read_text(encoding="utf-8", errors="replace")
    acks, errors, notes = parse_acks(message)
    _report_problems(errors, notes)
    open_sites = _verdict(_sites(["--cached"], ""), acks, branch=False)
    return 1 if (open_sites or errors) else 0


def _branch(head: str, base_ref: str, summary: str | None) -> int:
    base = _merge_base(base_ref, head)
    answered: list[tuple[str, Ack]] = []
    errors: list[str] = []
    for sha, message in _commits(base, head):
        found, problems, notes = parse_acks(message)
        answered.extend((sha, ack) for ack in found)
        errors.extend(f"{sha}: {problem}" for problem in problems)
        _report_problems((), tuple(f"{sha}: {note}" for note in notes))
    _report_problems(tuple(errors), ())
    sites = _sites([base, head], head)
    open_sites = _verdict(sites, tuple(ack for _, ack in answered), branch=True)
    if summary is not None:
        entries = [(sha, ack, answers(ack, sites)) for sha, ack in answered]
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(render_summary(entries, len(open_sites)) + "\n")
    return 1 if (open_sites or errors) else 0


def _report(base: str, head: str, *, as_json: bool) -> int:
    sites = _sites([base, head], head)
    for site in sites:
        if as_json:
            row = {
                "hint": site.hint.value,
                "path": site.path,
                "line": site.line,
                "detail": site.detail,
            }
            sys.stdout.write(json.dumps(row) + "\n")
        else:
            sys.stdout.write(f"{site.path}:{site.line}: hint {site.hint.value}: {site.detail}\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diff hints: questions a commit message answers.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("commit-msg").add_argument("message_file")
    branch = commands.add_parser("branch")
    branch.add_argument("--head", default="HEAD")
    branch.add_argument("--base", default="origin/main")
    branch.add_argument("--summary", default=None)
    stats = commands.add_parser("stats")
    stats.add_argument("base", nargs="?", default="origin/main")
    stats.add_argument("head", nargs="?", default="HEAD")
    stats.add_argument("--tree", action="store_true")
    stats.add_argument("--sites", type=int, default=0)
    report = commands.add_parser("report")
    report.add_argument("base")
    report.add_argument("head")
    report.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "commit-msg":
            return _commit_msg(args.message_file)
        if args.command == "branch":
            return _branch(args.head, args.base, args.summary)
        if args.command == "stats":
            return _stats(args.base, args.head, tree=args.tree, shown=args.sites)
        return _report(args.base, args.head, as_json=args.json)
    except (GitError, ValueError) as error:
        sys.stderr.write(f"diff hints: {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
