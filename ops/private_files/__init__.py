"""Read-only verification for registered private production files."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import cast

from base.paths.home import resolve_meiva_home
from ops.private_files.model import (
    CliArgs as CliArgs,
)
from ops.private_files.model import (
    GitSource as GitSource,
)
from ops.private_files.model import (
    ManifestEntry as ManifestEntry,
)
from ops.private_files.model import (
    Outcome as Outcome,
)
from ops.private_files.model import (
    OutcomeJson as OutcomeJson,
)
from ops.private_files.model import (
    RegistryManifest as RegistryManifest,
)
from ops.private_files.model import (
    relative_path as _relative_path,
)
from ops.private_files.model import (
    validate_manifest,
)

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


def _git_source_errors(
    source: GitSource, relative_path: PurePosixPath, expected_sha256: str
) -> list[str]:
    if source["kind"] != "git":
        raise ValueError(f"unsupported private-files source kind: {source['kind']!r}")
    repo = Path(source["repo"]).expanduser()
    if not repo.is_dir():
        return [f"source git repository is missing: {repo}"]

    object_name = f"{source['treeish']}:{relative_path.as_posix()}"
    exists = subprocess.run(  # noqa: S603 — fixed git plumbing with list-form arguments.
        ["git", "-C", str(repo), "cat-file", "-e", object_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode != 0:
        return [f"source git object is missing: {repo}@{object_name}"]

    blob = subprocess.run(  # noqa: S603 — fixed git plumbing with list-form arguments.
        ["git", "-C", str(repo), "cat-file", "blob", object_name],
        capture_output=True,
        check=False,
    )
    if blob.returncode != 0:
        return [f"source git object cannot be read: {repo}@{object_name}"]
    source_sha256 = hashlib.sha256(blob.stdout).hexdigest()
    if source_sha256 != expected_sha256:
        return [f"source sha256 mismatch: expected {expected_sha256}, got {source_sha256}"]
    return []


def verify(root: Path, manifest: Path) -> list[Outcome]:
    """Verify checked files and their durable git sources without writing either.

    Lifecycle semantics: an ``active`` entry must exist on disk AND match its
    durable git source — the missing-file guard. An ``archived`` entry's disk
    copy may be gone (the residue was removed after its source was archived);
    only the durable source must exist and match. This keeps an archived entry
    verifiable forever, instead of failing the moment the residue is deleted.
    """
    registry = _load_manifest(manifest)
    checked_root = root.expanduser()
    outcomes: list[Outcome] = []
    for entry in registry["entries"]:
        status = entry.get("status", "active")
        if status not in ("active", "archived"):
            raise ValueError(f"unsupported private-files entry status: {status!r}")
        relative_path = _relative_path(entry["path"])
        expected_sha256 = entry["sha256"]
        errors: list[str] = []
        if status == "active":
            checked_file = checked_root.joinpath(*relative_path.parts)
            if not checked_file.is_file():
                errors.append("file is missing")
            else:
                actual_sha256 = _file_sha256(checked_file)
                if actual_sha256 != expected_sha256:
                    errors.append(
                        f"file sha256 mismatch: expected {expected_sha256}, got {actual_sha256}"
                    )
        errors.extend(_git_source_errors(entry["source"], relative_path, expected_sha256))
        outcomes.append(
            Outcome(
                entry_id=entry["id"],
                path=relative_path.as_posix(),
                ok=not errors,
                errors=tuple(errors),
            )
        )
    return outcomes


def _default_root() -> Path:
    return resolve_meiva_home() / "source"


def _parse_args(argv: Sequence[str] | None) -> CliArgs:
    parser = argparse.ArgumentParser(description=__doc__)
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
    if cast(str, parsed.command) != "verify":
        raise AssertionError(f"unknown private-files command: {parsed.command!r}")
    root = cast(Path | None, parsed.root)
    return CliArgs(
        root=_default_root() if root is None else root,
        manifest=cast(Path, parsed.manifest),
        json_output=cast(bool, parsed.json_output),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    outcomes = verify(args.root, args.manifest)
    if args.json_output:
        sys.stdout.write(json.dumps([outcome.as_json() for outcome in outcomes]) + "\n")
    else:
        for outcome in outcomes:
            if outcome.ok:
                sys.stdout.write(f"OK {outcome.entry_id} {outcome.path}\n")
            else:
                sys.stdout.write(
                    f"FAIL {outcome.entry_id} {outcome.path}: {'; '.join(outcome.errors)}\n"
                )
    return 0 if all(outcome.ok for outcome in outcomes) else 1
