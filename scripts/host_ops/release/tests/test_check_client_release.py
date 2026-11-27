from __future__ import annotations

import pytest

from scripts.host_ops.release.check_client_release import check_client_release
from scripts.host_ops.release.github_releases import GitHubCommandError


@pytest.mark.parametrize(
    ("lookup", "latest", "error"),
    [
        ("existing", "client-v1.0.163", "existing release"),
        ("absent", "client-v1.0.163", "already holds Latest"),
        ("absent", "client-v1.0.162", "already holds Latest"),
        ("absent", "client-v1.0.99", ""),
        ("absent", "android-v1.0.1", ""),
        ("absent", None, ""),
        ("error", "client-v1.0.163", "lookup failed"),
    ],
)
def test_client_publication_preflight(lookup: str, latest: str | None, error: str) -> None:
    selectors: list[str] = []

    def gh(*args: str) -> str:
        match args[:2]:
            case ("api", "--paginate"):
                assert args[2:] == (
                    "repos/{owner}/{repo}/releases",
                    "--jq",
                    '.[] | select(.tag_name == "client-v1.0.162") | .id',
                )
                selectors.append("releases")
                if lookup == "error":
                    raise GitHubCommandError(1, args, "HTTP/2.0 503\n\n")
                return "123\n" if lookup == "existing" else ""
            case ("api", "--include"):
                assert args[2] == "repos/{owner}/{repo}/releases/latest"
                selectors.append("latest")
            case _:
                pytest.fail(f"unexpected GitHub command {args}")
        if latest is None:
            raise GitHubCommandError(1, args, "HTTP/2.0 404\n\n")
        value = latest
        return f"HTTP/2.0 200 OK\n\n{value}\n"

    if lookup == "error":
        with pytest.raises(GitHubCommandError):
            check_client_release(gh, "1.0.162")
    elif error:
        with pytest.raises(ValueError, match=error):
            check_client_release(gh, "1.0.162")
    else:
        check_client_release(gh, "1.0.162")

    expected = ["releases"]
    if lookup == "absent":
        expected.append("latest")
    assert selectors == expected
