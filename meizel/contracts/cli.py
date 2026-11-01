from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Collection, Sequence
from pathlib import Path

from meizel.checkout import GitRepo, read_roles, read_text, toplevel
from meizel.contracts import enumerations
from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.bootstrap import bootstrap
from meizel.contracts.debt_carry import renames_since, run_debt
from meizel.contracts.debt_sites import propagation
from meizel.contracts.explain import explain
from meizel.contracts.policy import Surface, load_policy, policy_at
from meizel.contracts.policy import changes as policy_changes
from meizel.contracts.sync import sync_manifests
from meizel.graph import ReachabilityScope, python_reverse_closure, reachability_analysis
from meizel.graph.model import Diagnostic, MeizelError
from meizel.source_evidence import Evidence, load_report

__all__ = ["main"]

# meiva:note
# This text is consumed by argparse as native CLI help, not discarded source prose.
# meiva:link meizel/docs/checker/checker.meiva.okf.md
_DESCRIPTION = (
    "Command line: check, explain, impacted, tests, sync --prune, bootstrap, debt and roles."
)

# meiva:note
# The installation this process runs from: the one seat that answers where the checker's own files
# are, read once here and handed down as a value (`source_evidence.Evidence`).
_TOOL_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# A repository-relative POSIX path for a path given relative to `base`.
def _relative_target(root: Path, base: Path, raw: str) -> str:
    resolved = (base / raw).resolve()
    try:
        rel = resolved.relative_to(root)
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


def _untracked_warning(untracked: Sequence[str]) -> str:
    sample = ", ".join(untracked[:3])
    return (
        f"meizel: warning: {len(untracked)} untracked source/manifest file(s) are invisible "
        f"to git-indexed analysis (e.g. {sample}); run 'git add -N {untracked[0]}' to include them"
    )


def _untracked_for_targets(
    untracked: Sequence[str], targets: list[str], directories: Collection[str]
) -> list[str]:
    if not targets:
        return list(untracked)
    return [
        p
        for p in untracked
        if _within(p, targets)
        or any(
            t and t not in directories and t.rpartition("/")[0] == p.rpartition("/")[0]
            for t in targets
        )
    ]


def _run_check(repo: GitRepo, root: Path, base: Path, args: argparse.Namespace) -> int:
    raw_paths: list[str] = args.paths
    changed: list[str] | None = args.changed
    if changed is not None and raw_paths:
        raise MeizelError("--changed judges the files it names; it cannot be combined with paths")
    targets = [_relative_target(root, base, p) for p in raw_paths]
    only = None if changed is None else frozenset(_relative_target(root, base, p) for p in changed)
    analysis = analyze(repo.universe(), jobs=args.jobs, changed=only)
    known = analysis.universe
    untracked = _untracked_for_targets(repo.untracked_files(), targets, known.dirs)
    if untracked:
        print(_untracked_warning(untracked), file=sys.stderr)
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
        _print_verdict(diagnostics, args.limit)
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
    impacted = commands.add_parser(
        "impacted",
        help="print tracked Python files in the reverse-import closure of changed files",
    )
    impacted.add_argument(
        "--python",
        action="store_true",
        help="select Python reverse-import closure (default)",
    )
    impacted.add_argument("paths", nargs="*", help="changed repository paths")
    tests_cmd = commands.add_parser(
        "tests",
        help="query derived static/native test lanes and changed-file impacted tests",
    )
    tests_cmd.add_argument(
        "--lane",
        choices=["static", "native"],
        help="filter or list collectable tests belonging to the static or native lane",
    )
    tests_cmd.add_argument(
        "--changed",
        nargs="*",
        metavar="path",
        help="select collectable tests impacted by these changed repository paths",
    )
    tests_cmd.add_argument(
        "--changed-files",
        metavar="file",
        help="read newline-delimited changed repository paths from this file",
    )
    tests_cmd.add_argument(
        "--base-ref",
        metavar="ref",
        help="git base ref for deleted-file recovery and strict public-interface cutoff",
    )
    tests_cmd.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print machine-readable JSON output",
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
    policy_surface = policy_modes.add_parser(
        "surface",
        help="report what the checker can still report, against a git ref",
        description=(
            "Compare the declarations that decide what the checker reports - meizel.policy.toml and "
            "every meizel.toml's [roots] and [state] - and print them as one markdown block. It "
            "lists a change and never fails on one: a narrowing still has to be visible here, "
            "because adding an allowance has no gate of its own."
        ),
    )
    policy_surface.add_argument("--against", required=True, metavar="ref")
    declared = commands.add_parser("roles", help="the declared top-level packages and their copies")
    modes = declared.add_subparsers(dest="mode", required=True)
    listing = modes.add_parser("list", help="print the top-level directories of the given roles")
    listing.add_argument("role_names", nargs="+", metavar="role")
    modes.add_parser("projects", help="print validated standalone projects for independent checks")
    modes.add_parser("check", help="fail on a stale generated region or a copy of a role's list")
    modes.add_parser("write", help="regenerate every generated region")
    reachability_cmd = commands.add_parser(
        "reachability",
        help="reachability analysis: identify disconnected islands and fake-test-only islands from [roots]",
    )
    reachability_cmd.add_argument("paths", nargs="*", help="report only islands under these paths")
    reachability_cmd.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print machine-readable JSON output",
    )
    reachability_cmd.add_argument(
        "--check",
        action="store_true",
        help="gate: fail (exit code 1) if unreachable or fake-test-only islands are found",
    )
    reachability_cmd.add_argument(
        "--roots",
        nargs="+",
        metavar="file",
        help="explicit production root paths (default: all [roots] in application manifests)",
    )
    reachability_cmd.add_argument(
        "--include-inits",
        action="store_true",
        help="include __init__.py files in reported islands (default: false)",
    )
    reachability_cmd.add_argument(
        "--limit",
        type=int,
        help="print at most this many diagnostics",
    )
    return parser


def _add_debt_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    debt = commands.add_parser(
        "debt",
        help="recorded locality debt (alone: a read-only summary)",
        description="Without a subcommand, summarise the baselines in the meizel.toml files.",
    )
    debt.add_argument("--json", action="store_true", help="print the summary as JSON")
    debt.add_argument(
        "--unowned",
        action="store_true",
        help="list the recorded debts that name no owner, one per line",
    )
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
    travelling = modes.add_parser(
        "propagation",
        help="measure the world reads that travel through a function (counted, not in the ledger)",
    )
    travelling.add_argument(
        "--top", type=int, default=10, help="how many of the widest wrappers to show"
    )
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
        "--symbol-moves-file",
        metavar="path",
        help=(
            "read `Symbol-Move: <old file>::<name> -> <new file> — <why>` lines from this file (the "
            "pull request record): each one lets the base's replay find the moved symbol at its new "
            "home, and the run reports every import it rewrote"
        ),
    )
    growth.add_argument(
        "--waive-if-changed",
        nargs="+",
        default=[],
        metavar="pathspec",
        help=(
            "extra git pathspecs that count as a rule change, on top of the ones debt diff detects by "
            "itself (the checker's rule code, meizel.policy.toml and every manifest's declarations): "
            "the change the new rules cause in the base's unchanged code is reported, never judged; "
            "the change the content causes always is"
        ),
    )


def _run_impacted(
    repo: GitRepo, root: Path, base: Path, raw_paths: list[str], jobs: int | None
) -> int:
    targets = [_relative_target(root, base, p) for p in raw_paths]
    reached = python_reverse_closure(repo.universe(), targets, jobs=jobs)
    if reached is None:
        raise MeizelError(
            "cannot compute impacted Python files: a tracked Python file could not be read or parsed"
        )
    for path in sorted(reached):
        print(path)
    return 0


# meiva:note
# `meizel debt ...`: the commands that read or write recorded debt, and the one that measures the
# world reads a baseline cannot hold. Kept out of `_dispatch` so the dispatch stays a list of commands.
def _run_debt(repo: GitRepo, root: Path, base: Path, args: argparse.Namespace) -> int:
    if args.debt_mode == "propagation":
        analysis = analyze(repo.universe(), jobs=args.jobs)
        print(
            propagation(analysis.universe, analysis.manifests, analysis.scans).render(top=args.top)
        )
        return 0
    targets = [_relative_target(root, base, p) for p in getattr(args, "paths", ())]
    return run_debt(repo, targets, args, args.jobs)


def _run_sync(repo: GitRepo, root: Path, base: Path, args: argparse.Namespace) -> int:
    if not (args.prune or args.declare):
        raise MeizelError("sync needs --prune or --declare")
    if args.paths and not args.declare:
        raise MeizelError("paths narrow --declare only")
    targets = [_relative_target(root, base, p) for p in args.paths] if args.paths else None
    untracked = repo.untracked_files()
    if untracked:
        print(_untracked_warning(untracked), file=sys.stderr)
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


def _run_surface(repo: GitRepo, against: str) -> int:
    # meiva:note
    # The base is a worktree of its own, so both sides are read by the same readers: the policy's
    # written for a historical tree, and the manifests' as they stand there. A ref whose tree cannot
    # be checked out is a run that cannot compare, and exits as one.
    with repo.checkout(against) as base:
        before = Surface.of(base.universe(), historical=True)
    print(before.report(Surface.of(repo.universe()), renames_since(repo, against), against=against))
    return 0


def _run_policy(repo: GitRepo, args: argparse.Namespace) -> int:
    if args.policy_mode == "surface":
        return _run_surface(repo, args.against)
    universe = repo.universe()
    for line in policy_changes(policy_at(universe, args.against), load_policy(universe)[0]):
        print(line)
    return 0


def _run_explain(repo: GitRepo, root: Path, base: Path, args: argparse.Namespace) -> int:
    analysis = analyze(repo.universe(), jobs=args.jobs)
    node = _relative_target(root, base, args.path)
    report = load_report(
        Evidence(root=root, tool=_TOOL_ROOT),
        set(analysis.universe.files),
        node,
        args.typescript_index,
    )
    print(explain(analysis, node, report))
    return 0


def _print_lane_query(repo: GitRepo, lane: str, *, as_json: bool) -> int:
    lanes = repo.test_lanes()
    chosen = sorted(lanes.static if lane == "static" else lanes.native)
    if as_json:
        payload: dict[str, object] = {"lane": lane, "count": len(chosen), "tests": chosen}
        if lane == "native":
            payload["reasons"] = dict(lanes.reasons)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for item in chosen:
        print(item)
    return 0


def _read_changed_targets(
    root: Path,
    base: Path,
    raw_changed: Sequence[str] | None,
    changed_file: str | None,
) -> list[str]:
    collected_raw: list[str] = list(raw_changed or ())
    if changed_file is not None:
        try:
            file_text = read_text(base, changed_file)
        except OSError as error:
            raise MeizelError(f"{changed_file}: cannot read changed-files list: {error}") from error
        collected_raw.extend(line.strip() for line in file_text.splitlines() if line.strip())
    return [_relative_target(root, base, p) for p in collected_raw]


def _print_selection_query(
    repo: GitRepo,
    rel_changed: Sequence[str],
    *,
    base_ref: str | None,
    lane: str | None,
    as_json: bool,
) -> int:
    selection = repo.select_tests(rel_changed, base_ref=base_ref)
    selected = list(selection.tests)
    if lane is not None:
        lanes = repo.test_lanes()
        allowed = lanes.static if lane == "static" else lanes.native
        selected = [t for t in selected if t in allowed]
    if as_json:
        print(
            json.dumps(
                {
                    "decision": selection.decision,
                    "reason": selection.reason,
                    "lane": lane,
                    "count": len(selected),
                    "tests": selected,
                    "diagnostics": list(selection.diagnostics),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if selection.decision == "FULL":
        print(f"meizel: full suite required ({selection.reason})", file=sys.stderr)
    for item in selected:
        print(item)
    return 0


def _run_tests(repo: GitRepo, root: Path, base: Path, args: argparse.Namespace) -> int:
    lane: str | None = args.lane
    raw_changed: list[str] | None = args.changed
    changed_file: str | None = args.changed_files
    if lane is None and raw_changed is None and changed_file is None:
        raise MeizelError("tests requires --lane, --changed, or --changed-files")
    if raw_changed is None and changed_file is None:
        return _print_lane_query(repo, str(lane), as_json=bool(args.as_json))
    rel_changed = _read_changed_targets(root, base, raw_changed, changed_file)
    return _print_selection_query(
        repo,
        rel_changed,
        base_ref=args.base_ref,
        lane=lane,
        as_json=bool(args.as_json),
    )


def _run_reachability(repo: GitRepo, root: Path, base: Path, args: argparse.Namespace) -> int:
    raw_paths: list[str] = args.paths
    targets = [_relative_target(root, base, p) for p in raw_paths]
    universe = repo.universe()
    analysis = analyze(universe, jobs=args.jobs)
    scope = ReachabilityScope(
        explicit_roots=args.roots,
        target_paths=targets if targets else None,
        include_inits=bool(getattr(args, "include_inits", False)),
    )
    report = reachability_analysis(
        universe,
        facts=analysis.facts,
        manifests=analysis.manifests,
        scope=scope,
    )
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return 1 if (args.check and not report.is_clean) else 0

    print(report.summary_line())
    shown = report.diagnostics if args.limit is None else report.diagnostics[: args.limit]
    for diagnostic in shown:
        print(diagnostic.render())
    if args.limit is not None and len(report.diagnostics) > len(shown):
        omitted = len(report.diagnostics) - len(shown)
        print(f"meizel: ({omitted} diagnostics not shown)", file=sys.stderr)

    if args.check and report.diagnostics:
        return 1
    return 0


def _dispatch(repo: GitRepo, root: Path, base: Path, args: argparse.Namespace) -> int:
    cmd = str(args.command)
    if cmd == "check":
        return _run_check(repo, root, base, args)
    if cmd == "debt":
        return _run_debt(repo, root, base, args)
    if cmd == "policy":
        return _run_policy(repo, args)
    if cmd == "explain":
        return _run_explain(repo, root, base, args)
    if cmd == "impacted":
        return _run_impacted(repo, root, base, args.paths, args.jobs)
    if cmd == "tests":
        return _run_tests(repo, root, base, args)
    if cmd == "reachability":
        return _run_reachability(repo, root, base, args)
    if cmd == "roles":
        return enumerations.run_roles(
            repo, read_roles(repo), args.mode, tuple(getattr(args, "role_names", ()))
        )
    if cmd == "sync":
        return _run_sync(repo, root, base, args)
    print(bootstrap(repo, args.out, args.jobs))
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
        return _dispatch(repo, repo.root.resolve(), base, args)
    except MeizelError as error:
        print(f"meizel: {error}", file=sys.stderr)
        return 2
