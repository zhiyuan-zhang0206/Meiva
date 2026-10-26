# meiva:note
# Read-only verification for registered private production files.
#
# The shell reads the manifest, the checked tree and the durable git sources, and hands what it
# found to `verdicts` and `report`. `main` is the one place that resolves the process home, and
# only when the operator named no root.

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from base.paths.home import resolve_meiva_home
from ops.private_files.model import (
    CliArgs,
    GitSource,
    Outcome,
    RegistryManifest,
    relative_path,
    validate_manifest,
)
from ops.private_files.report import exit_code, render
from ops.private_files.verdicts import GitProbe, disk_errors, requires_disk_file, source_errors

__all__ = ["main", "verify"]

_DEFAULT_MANIFEST = Path(__file__).with_name("manifest.json")
_HASH_CHUNK_SIZE = 1024 * 1024


def _load_manifest(path: Path) -> RegistryManifest:
    raw: object = json.loads(path.expanduser().read_text(encoding="utf-8"))
    return validate_manifest(raw)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


# meiva:note
# Run one fixed git plumbing command against `repo`; the arguments are list-form, never a shell
# string.
def _git(repo: Path, *args: str, capture: bool) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


# meiva:note
# Look at the durable git source of one entry, stopping at the first step that fails: the
# repository directory, then the object's existence, then its bytes.
def _probe_git_source(source: GitSource, relative: PurePosixPath) -> GitProbe:
    if source["kind"] != "git":
        raise ValueError(f"unsupported private-files source kind: {source['kind']!r}")
    repo = Path(source["repo"]).expanduser()
    object_name = f"{source['treeish']}:{relative.as_posix()}"
    if not repo.is_dir():
        return GitProbe(str(repo), object_name, repo_exists=False)
    if _git(repo, "cat-file", "-e", object_name, capture=False).returncode != 0:
        return GitProbe(str(repo), object_name, repo_exists=True, object_exists=False)
    blob = _git(repo, "cat-file", "blob", object_name, capture=True)
    if blob.returncode != 0:
        return GitProbe(
            str(repo), object_name, repo_exists=True, object_exists=True, blob_readable=False
        )
    return GitProbe(
        str(repo),
        object_name,
        repo_exists=True,
        object_exists=True,
        blob_readable=True,
        blob_sha256=hashlib.sha256(blob.stdout).hexdigest(),
    )


# meiva:note
# Verify checked files and their durable git sources without writing either.
#
# Lifecycle semantics: an ``active`` entry must exist on disk AND match its durable git source,
# the missing-file guard. An ``archived`` entry's disk copy may be gone (the residue was removed
# after its source was archived); only the durable source must exist and match. This keeps an
# archived entry verifiable forever, instead of failing the moment the residue is deleted.
def verify(root: Path, manifest: Path) -> list[Outcome]:
    registry = _load_manifest(manifest)
    checked_root = root.expanduser()
    outcomes: list[Outcome] = []
    for entry in registry["entries"]:
        relative = relative_path(entry["path"])
        expected_sha256 = entry["sha256"]
        errors: list[str] = []
        if requires_disk_file(entry.get("status", "active")):
            checked_file = checked_root.joinpath(*relative.parts)
            is_file = checked_file.is_file()
            errors.extend(
                disk_errors(
                    is_file=is_file,
                    actual_sha256=_file_sha256(checked_file) if is_file else None,
                    expected_sha256=expected_sha256,
                )
            )
        errors.extend(source_errors(_probe_git_source(entry["source"], relative), expected_sha256))
        outcomes.append(
            Outcome(
                entry_id=entry["id"],
                path=relative.as_posix(),
                ok=not errors,
                errors=tuple(errors),
            )
        )
    return outcomes


def _parse_args(argv: Sequence[str] | None) -> CliArgs:
    parser = argparse.ArgumentParser(
        description="Read-only verification for registered private production files."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify", help="verify every registered file")
    verify_parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="checked tree root (default: the home's source checkout, $MEIVA_HOME/source)",
    )
    verify_parser.add_argument(
        "--manifest",
        type=Path,
        default=_DEFAULT_MANIFEST,
        help=f"registry manifest (default: {_DEFAULT_MANIFEST})",
    )
    verify_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="emit machine-readable output"
    )
    parsed = parser.parse_args(argv)
    if parsed.command != "verify":
        raise AssertionError(f"unknown private-files command: {parsed.command!r}")
    root = parsed.root if isinstance(parsed.root, Path) else None
    manifest = parsed.manifest if isinstance(parsed.manifest, Path) else _DEFAULT_MANIFEST
    return CliArgs(
        root=root,
        manifest=manifest,
        json_output=bool(parsed.json_output),
    )


# meiva:note
# The operator entry (`python -m ops.private_files verify`): the one place that resolves the home,
# and only when no `--root` was given.
def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    root = args.root if args.root is not None else resolve_meiva_home() / "source"
    outcomes = verify(root, args.manifest)
    sys.stdout.write(render(outcomes, json_output=args.json_output))
    return exit_code(outcomes)
