from __future__ import annotations

import subprocess
from collections.abc import Callable


class GitHubCommandError(subprocess.CalledProcessError):
    def __init__(self, returncode: int, argv: tuple[str, ...], response: str) -> None:
        super().__init__(returncode, argv, output=response)
        self.response = response


def read_release_field(gh: Callable[..., str], selector: str, field: str) -> str | None:
    try:
        response = gh(
            "api",
            "--include",
            f"repos/{{owner}}/{{repo}}/releases/{selector}",
            "--jq",
            f'.{field} // error("missing {field}")',
        )
    except GitHubCommandError as error:
        status = error.response.split(maxsplit=2)
        if status[:1] and status[0].startswith("HTTP/") and status[1:2] == ["404"]:
            return None
        raise
    _, _, value = response.replace("\r\n", "\n").partition("\n\n")
    if not value.strip():
        raise ValueError(f"GitHub release {selector} returned no {field}")
    return value.strip()


def run_gh(*argv: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["gh", *argv], stdout=subprocess.PIPE, check=False, text=True
    )
    if result.returncode != 0:
        raise GitHubCommandError(result.returncode, argv, result.stdout)
    return result.stdout
