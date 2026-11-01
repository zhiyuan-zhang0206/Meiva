import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from base.host.proc import run_bounded
from scripts.content_lint.code_comments.check import Repository
from scripts.content_lint.code_comments.model import NoteKind, Source
from scripts.content_lint.code_comments.python_source import parse_python
from scripts.content_lint.code_comments.typescript_index import SUFFIXES, parsed_sources


def _brand_key(text: str) -> str:
    return (
        text.replace("meiva", "a" + "va")
        .replace("Meiva", "A" + "va")
        .replace("MEIVA", "A" + "VA")
        .replace("7d21fe712cd3", "947e545987bb")
    )


@dataclass(frozen=True)
class Snapshot:
    before: dict[str, str]
    after: dict[str, str]


def git(root: Path, *args: str) -> str:
    result = run_bounded(["git", *args], cwd=root, timeout=30, capture_output=True)
    if result.returncode:
        raise ValueError(f"Git comparison failed: {result.stderr.decode('utf-8').strip()}")
    return result.stdout.decode("utf-8")


def comparison_base(
    root: Path, explicit: str | None, start: str | None, end: str | None
) -> tuple[str, bool]:
    if explicit is not None:
        if start or end:
            raise ValueError("Choose a base or a from/to range, not both")
        return git(
            root, "rev-parse", "--verify", "--end-of-options", f"{explicit}^{{commit}}"
        ).strip(), False
    if start or end:
        if not start or not end:
            raise ValueError("Code comment comparison requires both from-ref and to-ref")
        base = git(root, "rev-parse", "--verify", "--end-of-options", f"{start}^{{commit}}").strip()
        target = git(root, "rev-parse", "--verify", "--end-of-options", f"{end}^{{commit}}").strip()
        if target != git(root, "rev-parse", "HEAD").strip():
            raise ValueError("Code comment range must end at the checked-out HEAD")
        return base, False
    return git(root, "rev-parse", "--verify", "HEAD").strip(), True


def require_snapshot(root: Path, *, staged: bool) -> None:
    arguments: list[tuple[str, ...]] = [("diff", "--quiet", "--no-ext-diff", "--")]
    if not staged:
        arguments.append(("diff", "--cached", "--quiet", "--no-ext-diff", "HEAD", "--"))
    for args in arguments:
        result = run_bounded(["git", *args], cwd=root, timeout=30, capture_output=True)
        if result.returncode:
            raise ValueError(
                "Code comment snapshot mismatch: tracked working tree must match the index, "
                "and committed comparisons require index and working tree to match HEAD; use the normal hook isolation"
            )


def tree_entries(root: Path, revision: str | None) -> dict[str, tuple[str, str]]:
    args = ("ls-files", "--stage", "-z") if revision is None else ("ls-tree", "-r", "-z", revision)
    entries: dict[str, tuple[str, str]] = {}
    for record in git(root, *args).split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        fields = metadata.split()
        if revision is None and fields[2] != "0":
            raise ValueError(f"unmerged index path: {path}")
        entries[path] = (fields[0], fields[1] if revision is None else fields[2])
    return entries


def _previous_blob(root: Path, old: dict[str, tuple[str, str]], path: str) -> str | None:
    old_mode, old_blob = old.get(path, ("", ""))
    if not old_mode and _brand_key(path) != path:
        old_mode, old_blob = old.get(_brand_key(path), ("", ""))
    if old_mode in {"100644", "100755"}:
        return git(root, "cat-file", "blob", old_blob)
    return None


def _diff_source_paths(repo: Repository, diff_args: tuple[str, ...]) -> list[str]:
    return [
        path
        for path in git(
            repo.root, *diff_args, "--no-renames", "--name-only", "-z", "--diff-filter=ACMRT", "--"
        ).split("\0")
        if path.endswith((".py", *SUFFIXES))
    ]


def _worktree_after(repo: Repository, paths: list[str]) -> dict[str, str]:
    after: dict[str, str] = {}
    for path in paths:
        target = repo.root / path
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"not a regular source file: {path}")
        after[path] = repo.text(path)
    return after


def _blob_after(repo: Repository, paths: list[str], *, staged: bool) -> dict[str, str]:
    new = tree_entries(repo.root, None if staged else "HEAD")
    after: dict[str, str] = {}
    for path in paths:
        repo.text(path)
        mode, blob = new[path]
        if mode not in {"100644", "100755"}:
            raise ValueError(f"not a regular source blob: {path}")
        after[path] = git(repo.root, "cat-file", "blob", blob)
    return after


# meiva:note
# Changed checks compare actual Git blobs from HEAD/index or a resolved base/HEAD range with a matching tracked checkout, unless `--worktree` explicitly compares tracked working-tree files against a base revision or HEAD for pre-stage local verification. Non-worktree runs reject mismatched snapshots, invalid targets and either-side parse failure instead of silently checking an empty or unrelated scope. Rename heuristics cannot reuse another path's legacy notes.
# meiva:link scripts/content_lint/code_comments/docs/incremental.meiva.okf.md
def snapshot(
    repo: Repository,
    base: str | None = None,
    *,
    from_ref: str | None = None,
    to_ref: str | None = None,
    worktree: bool = False,
) -> Snapshot:
    if worktree and (from_ref or to_ref):
        raise ValueError("Worktree comparison takes an optional base revision, not a from/to range")
    revision, staged = comparison_base(repo.root, base, from_ref, to_ref)
    if worktree:
        paths = _diff_source_paths(repo, ("diff", revision))
        after = _worktree_after(repo, paths)
    else:
        require_snapshot(repo.root, staged=staged)
        diff_args = ("diff", "--cached", revision) if staged else ("diff", revision, "HEAD")
        paths = _diff_source_paths(repo, diff_args)
        after = _blob_after(repo, paths, staged=staged)
    old = tree_entries(repo.root, revision)
    before = {
        path: previous
        for path in paths
        if (previous := _previous_blob(repo.root, old, path)) is not None
    }
    return Snapshot(before, after)


def parse_blobs(root: Path, contents: dict[str, str]) -> dict[str, Source]:
    sources = {
        path: parse_python(path, text) for path, text in contents.items() if path.endswith(".py")
    }
    scripts = {path: text for path, text in contents.items() if path.endswith(SUFFIXES)}
    if scripts:
        result = run_bounded(
            ["node", str(Path(__file__).with_name("typescript.mjs")), "--sources-json"],
            cwd=root,
            input=json.dumps(scripts),
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            stderr = result.stderr.strip()
            if "Cannot find module 'typescript'" in stderr:
                raise ValueError(
                    "TypeScript blob parser failed: this checkout has no installed frontend "
                    f"dependencies in {root / 'ui' / 'web'}, so the parser cannot load the "
                    "`typescript` the lockfile pins. Run `bash scripts/setup-worktree.sh` inside "
                    "this worktree, or `pnpm install --frozen-lockfile` in ui/web"
                )
            raise ValueError(f"TypeScript blob parser failed: {stderr}")
        sources.update(parsed_sources(result.stdout, scripts))
    return sources


# meiva:note
# Incremental strict checks consume exact old note text once within the same path and real lexical declaration identity. New copies, rewrites and cross-owner moves remain strict; unrelated code changes and local assignment renames do not force migration of unchanged notes. Global reference integrity is never grandfathered.
# meiva:link scripts/content_lint/code_comments/docs/incremental.meiva.okf.md
def new_note_indexes(source: Source, previous: Source | None) -> frozenset[int]:
    remaining: Counter[tuple[str, str, NoteKind, str]] = (
        Counter(
            (
                _brand_key(previous.path),
                _brand_key(note.lexical_owner),
                note.kind,
                _brand_key(note.raw_text),
            )
            for note in previous.notes
        )
        if previous is not None
        else Counter()
    )
    selected: set[int] = set()
    for index, note in enumerate(source.notes):
        key = (
            _brand_key(source.path),
            _brand_key(note.lexical_owner),
            note.kind,
            _brand_key(note.raw_text),
        )
        if remaining[key]:
            remaining[key] -= 1
        else:
            selected.add(index)
    return frozenset(selected)


def incremental_notes(
    repo: Repository,
    base: str | None = None,
    *,
    from_ref: str | None = None,
    to_ref: str | None = None,
    worktree: bool = False,
) -> dict[str, frozenset[int]]:
    comparison = snapshot(repo, base, from_ref=from_ref, to_ref=to_ref, worktree=worktree)
    before = parse_blobs(repo.root, comparison.before)
    after = parse_blobs(repo.root, comparison.after)
    repo.sources.update(after)
    return {path: new_note_indexes(source, before.get(path)) for path, source in after.items()}
