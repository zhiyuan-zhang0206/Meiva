from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Collection
from pathlib import Path

from meizel.checkout import GitRepo, toplevel
from meizel.contracts import enumerations
from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.bootstrap import bootstrap
from meizel.contracts.debt_carry import run_debt
from meizel.contracts.explain import explain
from meizel.contracts.policy import changes as policy_changes
from meizel.contracts.policy import load_policy, policy_at
from meizel.contracts.sync import sync_manifests
from meizel.graph.model import Diagnostic, MeizelError
from meizel.source_evidence import load_report

__all__ = ["main"]

# meiva:note
# This text is consumed by argparse as native CLI help, not discarded source prose.
# meiva:link meizel/docs/checker/checker.meiva.okf.md
_DESCRIPTION = "Command line: check, explain, sync --prune, bootstrap, debt and roles."


# meiva:note
# A repository-relative POSIX path for a path given relative to `base`.
def _relative_target(root: Path, base: Path, raw: str) -> str:
    resolved = (base / raw).resolve()
    try:
        rel = resolved.relative_to(root.resolve())
    except ValueError as error:
        raise MeizelError(f"{raw}: outside the repository {root}") from error
    return "" if str(rel) == "." else rel.as_posix()


def _within(path: str, targets: list[str]) -> bool:
    return any(not t or path == t or path.startswith(f"{t}/") for t in targets)


# meiva:note
# Whether a warning about `manifest` belongs to the run the caller asked for: the manifest sits at
# or under a target, or a target names a file in the manifest's own directory -- that file's
# manifest is the one being judged, so the table's warning is about the run that was asked for.
def _reports(manifest: str, targets: list[str], directories: Collection[str]) -> bool:
    if not targets or _within(manifest, targets):
        return True
    held = manifest.rpartition("/")[0]
    return any(t and t not in directories and t.rpartition("/")[0] == held for t in targets)


def _select(analysis: Analysis, targets: list[str]) -> list[Diagnostic]:
    unique = {(d.path, d.line, d.code, d.message): d for d in analysis.verdict}
    chosen = [d for d in unique.values() if not targets or _within(d.path, targets)]
    return sorted(chosen, key=lambda d: (d.path, d.line, d.code, d.message))


def _print_verdict(diagnostics: list[Diagnostic], limit: int | None) -> None:
    shown = diagnostics if limit is None else diagnostics[:limit]
    for diagnostic in shown:
        print(diagnostic.render())
    counts = Counter(d.code for d in diagnostics)
    detail = ", ".join(f"{code}: {n}" for code, n in sorted(counts.items()))
    omitted = (
        f" ({len(diagnostics) - len(shown)} not shown)" if len(shown) < len(diagnostics) else ""
    )
    print(f"meizel: {len(diagnostics)} violations{omitted} [{detail}]", file=sys.stderr)


def _run_check(
    repo: GitRepo,
    base: Path,
    raw_paths: list[str],
    limit: int | None,
    jobs: int | None,
    changed: list[str] | None = None,
) -> int:
    if changed is not None and raw_paths:
        raise MeizelError("--changed judges the files it names; it cannot be combined with paths")
    root = repo.root
    targets = [_relative_target(root, base, p) for p in raw_paths]
    only = None if changed is None else frozenset(_relative_target(root, base, p) for p in changed)
    analysis = analyze(repo.universe(), jobs=jobs, changed=only)
    known = analysis.universe
    for raw, target in zip(raw_paths, targets, strict=True):
        if target and target not in known.files and target not in known.dirs:
            raise MeizelError(f"{raw}: not a tracked file or directory")
    diagnostics = _select(analysis, targets)
    print(analysis.coverage_line())
    # meiva:note
    # Warnings report a declaration the run cannot honour; they never change the verdict, because
    # no rule is violated by writing a table the policy does not reach. A file target names a file
    # whose directory the table claims to govern, so it reports that directory's warning too.
    for manifest, warning in analysis.inert:
        if _reports(manifest, targets, known.dirs):
            print(warning, file=sys.stderr)
    if diagnostics:
        _print_verdict(diagnostics, limit)
        return 1
    print(analysis.ok_line())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m meizel", description=_DESCRIPTION)
    parser.add_argument(
        "--repo", type=Path, help="repository root (default: the cwd's git toplevel)"
    )
    parser.add_argument("--jobs", type=int, help="worker processes for parsing (default: up to 8)")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="gate: fail on any violation")
    check.add_argument("paths", nargs="*", help="report only diagnostics under these paths")
    check.add_argument("--limit", type=int, help="print at most this many diagnostics")
    check.add_argument(
        "--changed",
        nargs="+",
        metavar="file",
        help="judge these files' facts, declared graphs and global role lists; skip stale graph checks",
    )
    show = commands.add_parser("explain", help="show a node's view of the graph")
    show.add_argument("path")
    show.add_argument(
        "--typescript-index",
        type=Path,
        help="explicit additional holder-evidence index; ordinary analysis still uses the compiler",
    )
    sync = commands.add_parser("sync", help="maintain manifests")
    sync.add_argument("--prune", action="store_true", help="remove stale declared edges")
    sync.add_argument(
        "--declare",
        action="store_true",
        help="declare the legal new dependencies of the paths (default: the changed files); also prunes",
    )
    sync.add_argument("paths", nargs="*", help="with --declare: only dependencies of these paths")
    seed = commands.add_parser("bootstrap", help="generate manifests from current facts")
    seed.add_argument("--out", type=Path, required=True, help="directory to mirror manifests into")
    _add_debt_parser(commands)
    policy = commands.add_parser("policy", help="the allowance lists in meizel.policy.toml")
    policy_modes = policy.add_subparsers(dest="policy_mode", required=True)
    policy_diff = policy_modes.add_parser("diff", help="list the entries changed against a git ref")
    policy_diff.add_argument("--against", required=True, metavar="ref")
    declared = commands.add_parser("roles", help="the declared top-level packages and their copies")
    modes = declared.add_subparsers(dest="mode", required=True)
    listing = modes.add_parser("list", help="print the top-level directories of the given roles")
    listing.add_argument("role_names", nargs="+", metavar="role")
    modes.add_parser("check", help="fail on a stale generated region or a copy of a role's list")
    modes.add_parser("write", help="regenerate every generated region")
    return parser


def _add_debt_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    debt = commands.add_parser(
        "debt",
        help="recorded locality debt (alone: a read-only summary)",
        description="Without a subcommand, summarise the baselines in the meizel.toml files.",
    )
    debt.add_argument("--json", action="store_true", help="print the summary as JSON")
    modes = debt.add_subparsers(dest="debt_mode")
    recording = modes.add_parser(
        "record", help="write the current violations under paths into baselines"
    )
    recording.add_argument("paths", nargs="+")
    modes.add_parser("prune", help="remove recorded debt that no longer occurs")
    resolving = modes.add_parser(
        "resolve", help="resolve merge conflicts inside the [debt] tables of manifests"
    )
    resolving.add_argument("paths", nargs="+")
    moving = modes.add_parser(
        "carry", help="move recorded debt to where renamed files, targets and symbols now are"
    )
    moving.add_argument(
        "--rename-target",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="a patch target that was renamed (a symbol, not a file); repeatable",
    )
    moving.add_argument(
        "--move-symbol",
        action="append",
        default=[],
        metavar="OLD_FILE::NAME=NEW_FILE",
        help="a top-level function or class that moved to another file; repeatable",
    )
    moving.add_argument(
        "--base",
        metavar="ref",
        help="compare with this ref (default: the branch point with origin/main)",
    )
    listing = modes.add_parser(
        "sites", help="list the places in the source that a patch code names"
    )
    listing.add_argument("--code", required=True, help="a patch code, e.g. first-party-patch")
    listing.add_argument(
        "--target-prefix",
        action="append",
        default=[],
        metavar="prefix",
        help="only patches whose dotted target is this or inside it; repeatable",
    )
    listing.add_argument("--json", dest="as_json", action="store_true", help="print JSON")
    listing.add_argument("paths", nargs="*", help="only files under these paths")
    approving = modes.add_parser(
        "approved", help="list the files approved to patch, with the patches each holds now"
    )
    approving.add_argument("--json", dest="as_json", action="store_true", help="print JSON")
    growth = modes.add_parser("diff", help="compare recorded totals with a git ref")
    growth.add_argument("--against", required=True, metavar="ref")
    growth.add_argument("--on-growth", required=True, choices=["fail", "warn"])
    growth.add_argument(
        "--by-target",
        action="store_true",
        help="also list the recorded patch debt per target group: what the change paid down or added",
    )
    growth.add_argument(
        "--target-prefix",
        action="append",
        default=[],
        metavar="prefix",
        help="with --by-target: a group, as a dotted target prefix; repeatable (default: by --depth)",
    )
    growth.add_argument(
        "--depth",
        type=int,
        default=3,
        help="with --by-target and no prefix: how many dotted names of a target form a group",
    )
    growth.add_argument(
        "--waive-if-changed",
        nargs="+",
        default=[],
        metavar="pathspec",
        help=(
            "a change touching these git pathspecs (the rule code) is a rule change: the growth the "
            "new rules cause in the base's unchanged code is reported, never judged; the growth the "
            "content causes always is"
        ),
    )


def _run_sync(repo: GitRepo, base: Path, args: argparse.Namespace) -> int:
    if not (args.prune or args.declare):
        raise MeizelError("sync needs --prune or --declare")
    if args.paths and not args.declare:
        raise MeizelError("paths narrow --declare only")
    root = repo.root
    targets = [_relative_target(root, base, p) for p in args.paths] if args.paths else None
    lines, left = sync_manifests(repo, targets, args.jobs, do_declare=bool(args.declare))
    for line in lines:
        print(line)
    for line in left:
        print(line)
    if left:
        print(
            f"meizel: {len(left)} dependencies need a code change, not a declaration",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        base = args.repo if args.repo is not None else Path.cwd()
        # meiva:note
        # The one repository seat of the command: this checkout, read through one value that the
        # commands are handed instead of a path they would build their own from. A command that
        # judges reads a snapshot of it (`repo.universe()`).
        repo = GitRepo(args.repo if args.repo is not None else toplevel(base))
        root = repo.root
        if args.command == "check":
            return _run_check(repo, base, args.paths, args.limit, args.jobs, args.changed)
        if args.command == "debt":
            targets = [_relative_target(root, base, p) for p in getattr(args, "paths", ())]
            return run_debt(repo, targets, args, args.jobs)
        if args.command == "policy":
            universe = repo.universe()
            for line in policy_changes(policy_at(universe, args.against), load_policy(universe)[0]):
                print(line)
            return 0
        if args.command == "explain":
            analysis = analyze(repo.universe(), jobs=args.jobs)
            node = _relative_target(root, base, args.path)
            report = load_report(root, set(analysis.universe.files), node, args.typescript_index)
            print(explain(analysis, node, report))
            return 0
        if args.command == "roles":
            return enumerations.run_roles(repo, args.mode, tuple(getattr(args, "role_names", ())))
        if args.command == "sync":
            return _run_sync(repo, base, args)
        print(bootstrap(repo, args.out, args.jobs))
        return 0
    except MeizelError as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2
