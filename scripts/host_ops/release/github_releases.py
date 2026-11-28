from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path


class GitHubCommandError(subprocess.CalledProcessError):
    def __init__(self, returncode: int, argv: tuple[str, ...], response: str) -> None:
        super().__init__(returncode, argv, output=response)
        self.response = response


def read_release_field(gh: Callable[..., str], selector: str, field: str) -> str | None:
    return read_release_query(gh, selector, f'.{field} // error("missing {field}")')


def read_release_query(gh: Callable[..., str], selector: str, query: str) -> str | None:
    try:
        response = gh(
            "api",
            "--include",
            f"repos/{{owner}}/{{repo}}/releases/{selector}",
            "--jq",
            query,
        )
    except GitHubCommandError as error:
        status = error.response.split(maxsplit=2)
        if status[:1] and status[0].startswith("HTTP/") and status[1:2] == ["404"]:
            return None
        raise
    _, _, value = response.replace("\r\n", "\n").partition("\n\n")
    if not value.strip():
        raise ValueError(f"GitHub release {selector} returned an empty response")
    return value.strip("\n")


def run_gh(*argv: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["gh", *argv], stdout=subprocess.PIPE, check=False, text=True
    )
    if result.returncode != 0:
        raise GitHubCommandError(result.returncode, argv, result.stdout)
    return result.stdout


def asset_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def uploaded_assets(gh: Callable[..., str], release_id: str) -> dict[str, str]:
    rows = gh(
        "api",
        "--paginate",
        f"repos/{{owner}}/{{repo}}/releases/{release_id}/assets",
        "--jq",
        '.[] | [.name, (if .state == "uploaded" then .digest else "" end)] | @tsv',
    )
    return asset_rows(rows)


def asset_rows(rows: str) -> dict[str, str]:
    assets: dict[str, str] = {}
    for line in rows.splitlines():
        name, separator, digest = line.partition("\t")
        if not separator or not name or name in assets:
            raise ValueError(f"invalid release asset row: {line!r}")
        assets[name] = digest
    return assets


def download_asset(gh: Callable[..., str], tag: str, path: Path, expected: str) -> None:
    _ = gh("release", "download", tag, "--pattern", path.name, "--output", str(path))
    if asset_digest(path) != expected:
        raise ValueError(f"{tag} returned an unverified {path.name}")
