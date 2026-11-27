from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


class _Arguments(argparse.Namespace):
    tag: str
    title: str
    notes: str
    files: list[Path]


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def publish_feed(
    gh: Callable[..., str], *, tag: str, files: Sequence[Path], title: str, notes: str
) -> None:
    if len(files) < 2 or any(path.stat().st_size == 0 for path in files):
        raise ValueError("expected non-empty binary assets and a manifest")
    expected = {path.name: _digest(path) for path in files}
    _ = gh("release", "upload", tag, *(str(path) for path in files[:-1]), "--clobber")
    _ = gh("release", "upload", tag, str(files[-1]), "--clobber")
    release_id = gh("release", "view", tag, "--json", "databaseId", "--jq", ".databaseId").strip()
    rows = gh(
        "api",
        "--paginate",
        f"repos/{{owner}}/{{repo}}/releases/{release_id}/assets",
        "--jq",
        '.[] | [.name, (if .state == "uploaded" then .digest else "" end)] | @tsv',
    )
    uploaded = dict(line.split("\t", maxsplit=1) for line in rows.splitlines())
    if any(uploaded.get(name) != digest for name, digest in expected.items()):
        raise ValueError(f"{tag} is missing one or more newly uploaded assets")
    for name in sorted(uploaded.keys() - expected.keys()):
        _ = gh("release", "delete-asset", tag, name, "--yes")
    _ = gh(
        "release", "edit", tag, "--prerelease", "--latest=false", "--title", title, "--notes", notes
    )


def _gh(*argv: str) -> str:
    return subprocess.check_output(["gh", *argv], text=True)  # noqa: S603


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Publish and reconcile a mutable client updater feed."
    )
    _ = parser.add_argument("--tag", required=True, choices=["app-latest", "android-latest"])
    _ = parser.add_argument("--title", required=True)
    _ = parser.add_argument("--notes", required=True)
    _ = parser.add_argument(
        "files", type=Path, nargs="+", help="binary assets followed by the manifest"
    )
    args = parser.parse_args(argv, namespace=_Arguments())
    publish_feed(_gh, tag=args.tag, files=args.files, title=args.title, notes=args.notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
