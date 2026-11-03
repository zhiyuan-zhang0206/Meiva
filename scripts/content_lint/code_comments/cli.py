import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from base.host.boundary.options import attribute, options
from meizel.checkout import load_universe, toplevel
from meizel.graph.model import MeizelError
from scripts.content_lint.code_comments import corpus as corpus_module
from scripts.content_lint.code_comments.changes import incremental_notes
from scripts.content_lint.code_comments.check import SOURCE_SUFFIXES, Repository, Result, check
from scripts.content_lint.code_comments.typescript_index import load_index


def selected_paths(repo: Repository, raw: Sequence[str]) -> frozenset[str]:
    if not raw:
        return frozenset(path for path in repo.files if path.endswith(SOURCE_SUFFIXES))
    selected: set[str] = set()
    for item in raw:
        path = (repo.root / item).resolve().relative_to(repo.root.resolve()).as_posix()
        candidates = {file for file in repo.files if file == path or file.startswith(f"{path}/")}
        if not candidates:
            raise ValueError(f"no tracked files under {item}")
        if path in repo.files and not path.endswith(SOURCE_SUFFIXES):
            raise ValueError(
                f"no source parser for {path}; select supported source files or a directory"
            )
        selected.update(file for file in candidates if file.endswith(SOURCE_SUFFIXES))
    if not selected:
        raise ValueError("source check selected no supported source files")
    return frozenset(selected)


def report_result(
    result: Result,
    command: str,
    paths: frozenset[str] | None,
    notes: dict[str, frozenset[int]] | None,
    *,
    json_output: bool,
) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "mode": command,
                    "notes": result.notes,
                    "strict_source_files": len(paths) if paths is not None else 0,
                    "new_or_rewritten_notes": sum(map(len, notes.values()))
                    if notes is not None
                    else None,
                    "findings": [asdict(item) for item in result.findings],
                },
                indent=2,
            )
        )
        return
    for finding in result.findings:
        print(finding.render())
    scope = (
        "links only; free-comment migration is not checked"
        if paths is None
        else f"{sum(map(len, notes.values()))} new or rewritten notes in {len(paths)} changed sources; links and prose outside them are not rechecked"
        if notes is not None
        else f"{len(paths)} source files checked for free comments"
    )
    counts = dict(sorted(Counter(item.code for item in result.findings).items()))
    print(f"code comments: {result.notes} notes; {scope}; {counts}")


# meiva:note
# What every subcommand takes (the flags before the subcommand name). The subcommand's own flags are
# read by the dataclass below that names them, only on the path that runs it.
@dataclass(frozen=True)
class CommonOptions:
    repo: Path | None
    json: bool
    typescript_index: str | None


@dataclass(frozen=True)
class CheckOptions:
    paths: list[str]


@dataclass(frozen=True)
class ChangedOptions:
    base: str | None
    from_ref: str | None
    to_ref: str | None
    worktree: bool


@dataclass(frozen=True)
class CorpusOptions:
    format: str
    output: Path | None


# meiva:note
# The notes a `changed` run judges: those the Git comparison its flags describe adds or rewrites.
def _changed_notes(repo: Repository, args: argparse.Namespace) -> dict[str, frozenset[int]]:
    changed = options(args, ChangedOptions)
    return incremental_notes(
        repo,
        changed.base,
        from_ref=changed.from_ref,
        to_ref=changed.to_ref,
        worktree=changed.worktree,
    )


# meiva:note
# The files a run checks strictly: the changed sources of a `changed` run, the paths a `check` names
# (every source file when none), every source file for `inventory`, and none for `validate`/`corpus`.
def _selection(
    repo: Repository, args: argparse.Namespace, notes: dict[str, frozenset[int]] | None
) -> frozenset[str] | None:
    if notes is not None:
        return frozenset(notes)
    command = attribute(args, "command", str)
    if command == "check":
        return selected_paths(repo, options(args, CheckOptions).paths)
    if command == "inventory":
        return selected_paths(repo, [])
    return None


def _write_corpus(repo: Repository, result: Result, corpus: CorpusOptions) -> int:
    rows = corpus_module.entries(repo)
    content = (
        corpus_module.jsonl(rows) if corpus.format == "jsonl" else corpus_module.markdown(rows)
    )
    if corpus.output is not None:
        corpus_module.write(corpus.output, content)
        print(f"code comments: {len(rows)} notes written to {corpus.output}", file=sys.stderr)
    else:
        print(content, end="")
    for finding in result.findings:
        print(finding.render(), file=sys.stderr)
    return int(bool(result.findings))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Parseable source notes with links to tracked repository files"
    )
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--typescript-index", type=str, help="parser output path, or - for stdin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate every source note link; does not check prose")
    strict = commands.add_parser(
        "check", help="also reject every free comment, docstring and unused string in the selection"
    )
    strict.add_argument("paths", nargs="*")
    commands.add_parser(
        "inventory",
        help="report outstanding migration findings; exit nonzero while any remain",
    )
    changed = commands.add_parser(
        "changed",
        help="check new or rewritten notes against Git in the changed sources, links included",
    )
    changed.add_argument("base", nargs="?")
    changed.add_argument("--from-ref")
    changed.add_argument("--to-ref")
    changed.add_argument(
        "--worktree",
        action="store_true",
        help="compare tracked working-tree files against base or HEAD without requiring a clean index",
    )
    dump = commands.add_parser("corpus", help="extract every source note for review")
    dump.add_argument("--format", choices=["jsonl", "markdown"], default="jsonl")
    dump.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    common = options(args, CommonOptions)
    command = attribute(args, "command", str)
    try:
        root = common.repo.resolve() if common.repo is not None else toplevel(Path.cwd())
        repo = Repository(root, load_universe(root).files)
        if common.typescript_index is not None:
            content = (
                sys.stdin.read()
                if common.typescript_index == "-"
                else Path(common.typescript_index).read_text(encoding="utf-8")
            )
            load_index(repo, content)
        notes = _changed_notes(repo, args) if command == "changed" else None
        paths = _selection(repo, args, notes)
        result = check(
            repo,
            scan=frozenset(notes) if notes is not None else None,
            strict_paths=paths,
            strict_notes=notes,
        )
        if command == "corpus":
            return _write_corpus(repo, result, options(args, CorpusOptions))
        report_result(result, command, paths, notes, json_output=common.json)
        return int(bool(result.findings))
    except (OSError, ValueError, TypeError, SyntaxError, MeizelError) as error:
        print(f"code comments: cannot complete check: {error}", file=sys.stderr)
        return 2
