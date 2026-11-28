from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.host_ops.release.github_releases import GitHubCommandError, asset_digest
from scripts.host_ops.release.publish_client_release import publish_client_release

_TAG = "client-v1.0.204"
_LATEST = "client-v1.0.203"
_RELEASES = "repos/{owner}/{repo}/releases"
_RELEASE = f"{_RELEASES}/42"
_LIST = ("api", "--paginate", _RELEASES)
_ASSETS = ("api", "--paginate", f"{_RELEASE}/assets")
_VERIFY_TAG = ("api", f"repos/{{owner}}/{{repo}}/git/ref/tags/{_TAG}", "--jq", ".ref")
_CREATE = (
    "api",
    "--method",
    "POST",
    _RELEASES,
    "-f",
    f"tag_name={_TAG}",
    "-f",
    "name=Meiva App 1.0.204",
    "-f",
    "body=release",
    "-F",
    "draft=true",
    "--jq",
    "[.id, .upload_url] | @tsv",
)
_READ_DRAFT = (
    "api",
    "--include",
    _RELEASE,
    "--jq",
    "[.tag_name, .draft, .upload_url] | @tsv",
)
_UPLOAD_URL = "https://uploads.github.com/repos/owner/Meiva/releases/42/assets"
_PUBLISH = (
    "api",
    "--method",
    "PATCH",
    _RELEASE,
    "-F",
    "draft=false",
    "-f",
    "make_latest=true",
)
_DELETE_ASSET = ("api", "--method", "DELETE", f"{_RELEASES}/assets/99")


@pytest.fixture
def staged_files(tmp_path: Path) -> Path:
    for name in (
        "Meiva_1.0.204_universal.dmg",
        "Meiva_1.0.204.app.tar.gz",
        "Meiva_1.0.204.app.tar.gz.sig",
        "latest.json",
        "meiva-app-1.0.203.apk",
        "android-latest.json",
    ):
        _ = (tmp_path / name).write_text(name)
    return tmp_path


class _GitHub:
    def __init__(self, draft: bool | None = None) -> None:
        self.draft = draft
        self.tag = _TAG
        self.tag_exists = True
        self.created = False
        self.assets: dict[str, tuple[int, str, str]] = {}
        self.change_on_asset_read = ""
        self.corrupt_uploads = False
        self.move_latest_on_upload = False
        self.latest = _LATEST
        self.calls: list[tuple[str, ...]] = []

    def asset_rows(self) -> str:
        if self.change_on_asset_read == "publish":
            self.draft = False
        if self.change_on_asset_read == "retag":
            self.tag = "client-v1.0.999"
        if self.change_on_asset_read == "delete":
            self.draft = None
        return "\n".join(
            f"{name}\t{asset_id}\t{state}\t{digest}"
            for name, (asset_id, state, digest) in self.assets.items()
        )

    def upload(self, args: tuple[str, ...]) -> None:
        assert args[4:7] == ("--header", "Content-Type: application/octet-stream", "--input")
        path = Path(args[7])
        assert parse_qs(urlsplit(args[3]).query) == {"name": [path.name]}
        assert path.name not in self.assets
        digest = "corrupt" if self.corrupt_uploads else asset_digest(path)
        self.assets[path.name] = (100 + len(self.assets), "uploaded", digest)
        if self.move_latest_on_upload:
            self.latest = "client-v1.0.205"

    def stale_release_index(self) -> str:
        if self.created or self.draft is None:
            return ""
        return f"42\t{str(self.draft).lower()}\n"

    def read_draft(self, upload_template: str) -> str:
        if self.draft is None:
            raise GitHubCommandError(1, _READ_DRAFT, "HTTP/2 404\n\n")
        return f"HTTP/2 200\n\n{self.tag}\t{str(self.draft).lower()}\t{upload_template}\n"

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        upload_template = _UPLOAD_URL + "{?name,label}"
        match args:
            case ("api", "--include", "repos/{owner}/{repo}/releases/latest", *_):
                return f"HTTP/2 200\n\n{self.latest}\n"
            case _ if args[:3] == _LIST:
                return self.stale_release_index()
            case _ if args[:3] == _ASSETS:
                return self.asset_rows()
            case _ if args == _VERIFY_TAG:
                if not self.tag_exists:
                    raise GitHubCommandError(1, args, "HTTP/2 404\n\n")
                return f"refs/tags/{_TAG}\n"
            case _ if args == _CREATE:
                assert self.tag_exists
                self.created = True
                self.draft = True
                return f"42\t{upload_template}\n"
            case _ if args == _READ_DRAFT:
                return self.read_draft(upload_template)
            case _ if args == _DELETE_ASSET:
                del self.assets["Meiva_1.0.204.app.tar.gz"]
            case ("api", "--method", "POST", url, *_) if url.startswith(_UPLOAD_URL + "?"):
                self.upload(args)
            case _ if args == _PUBLISH:
                assert self.draft is True
                self.draft = False
                self.latest = self.tag
            case _:
                raise AssertionError(args)
        return ""


def _publish(gh: _GitHub, directory: Path) -> None:
    publish_client_release(
        gh, version="1.0.204", directory=directory, latest=_LATEST, notes="release"
    )


def _uploads(gh: _GitHub) -> list[tuple[str, ...]]:
    return [call for call in gh.calls if len(call) > 3 and call[3].startswith(_UPLOAD_URL + "?")]


@pytest.mark.parametrize("state", ["new", "partial draft", "starter"])
def test_only_a_verified_draft_can_become_latest(staged_files: Path, state: str) -> None:
    gh = _GitHub(draft=True if state != "new" else None)
    if state == "partial draft":
        dmg = staged_files / "Meiva_1.0.204_universal.dmg"
        gh.assets[dmg.name] = (98, "uploaded", asset_digest(dmg))
    if state == "starter":
        gh.assets["Meiva_1.0.204.app.tar.gz"] = (99, "starter", "")

    _publish(gh, staged_files)

    assert sum(call[:3] == _LIST for call in gh.calls) == 1
    assert gh.calls[-2:] == [_READ_DRAFT, _PUBLISH]
    assert gh.draft is False
    assert gh.latest == _TAG
    uploads = _uploads(gh)
    assert len(uploads) == 6 - (state == "partial draft")
    if state == "new":
        assert gh.calls.index(_VERIFY_TAG) < gh.calls.index(_CREATE) < gh.calls.index(uploads[0])
    if state == "starter":
        assert (
            gh.calls.index(_READ_DRAFT) < gh.calls.index(_DELETE_ASSET) < gh.calls.index(uploads[0])
        )


def test_missing_tag_refuses_creation(staged_files: Path) -> None:
    gh = _GitHub()
    gh.tag_exists = False

    with pytest.raises(GitHubCommandError):
        _publish(gh, staged_files)

    assert gh.calls[-1] == _VERIFY_TAG
    assert _CREATE not in gh.calls


def test_refuses_published_release_found_by_tag(staged_files: Path) -> None:
    gh = _GitHub(draft=False)

    with pytest.raises(ValueError, match="refusing to modify published release"):
        _publish(gh, staged_files)

    assert gh.calls[-1][:3] == _LIST


@pytest.mark.parametrize("change", ["publish", "retag", "delete"])
@pytest.mark.parametrize("stage", ["starter cleanup", "publication"])
def test_refuses_changed_draft_by_id(staged_files: Path, change: str, stage: str) -> None:
    gh = _GitHub(draft=True if stage == "starter cleanup" else None)
    gh.change_on_asset_read = change
    if stage == "starter cleanup":
        gh.assets["Meiva_1.0.204.app.tar.gz"] = (99, "starter", "")

    with pytest.raises(ValueError):
        _publish(gh, staged_files)

    assert gh.calls[-1] == _READ_DRAFT
    assert _DELETE_ASSET not in gh.calls
    assert _PUBLISH not in gh.calls
    assert len(_uploads(gh)) == (6 if stage == "publication" else 0)


@pytest.mark.parametrize("anomaly", ["digest", "name", "state"])
def test_rejects_other_invalid_assets_before_mutating_the_draft(
    staged_files: Path, anomaly: str
) -> None:
    gh = _GitHub(draft=True)
    dmg = staged_files / "Meiva_1.0.204_universal.dmg"
    gh.assets["Meiva_1.0.204.app.tar.gz"] = (99, "starter", "")
    gh.assets["unexpected" if anomaly == "name" else dmg.name] = (
        98,
        "new" if anomaly == "state" else "uploaded",
        "corrupt" if anomaly == "digest" else asset_digest(dmg),
    )

    with pytest.raises(ValueError, match="unexpected assets or digests"):
        _publish(gh, staged_files)

    assert all(call[:2] in {("api", "--include"), ("api", "--paginate")} for call in gh.calls)


@pytest.mark.parametrize("anomaly", ["bad digest", "changed Latest"])
def test_checks_the_uploaded_draft_again_before_publication(
    staged_files: Path, anomaly: str
) -> None:
    gh = _GitHub()
    gh.corrupt_uploads = anomaly == "bad digest"
    gh.move_latest_on_upload = anomaly == "changed Latest"

    with pytest.raises(ValueError):
        _publish(gh, staged_files)

    assert len(_uploads(gh)) == 6
    assert _PUBLISH not in gh.calls
