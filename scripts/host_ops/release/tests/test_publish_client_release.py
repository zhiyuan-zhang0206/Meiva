from pathlib import Path

import pytest

from scripts.host_ops.release.github_releases import asset_digest
from scripts.host_ops.release.publish_client_release import publish_client_release

_TAG = "client-v1.0.204"
_LATEST = "client-v1.0.203"
_DELETE_ASSET = ("api", "--method", "DELETE", "repos/{owner}/{repo}/releases/assets/99")


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
        self.assets: dict[str, tuple[int, str, str]] = {}
        self.publish_after_listing = False
        self.corrupt_uploads = False
        self.move_latest_on_upload = False
        self.latest = _LATEST
        self.calls: list[tuple[str, ...]] = []

    def asset_rows(self) -> str:
        if self.publish_after_listing:
            self.draft = False
        return "\n".join(
            f"{name}\t{asset_id}\t{state}\t{digest}"
            for name, (asset_id, state, digest) in self.assets.items()
        )

    def upload(self, files: tuple[str, ...]) -> None:
        for index, item in enumerate(files, 100):
            path = Path(item)
            assert path.name not in self.assets
            digest = "corrupt" if self.corrupt_uploads else asset_digest(path)
            self.assets[path.name] = (index, "uploaded", digest)
        if self.move_latest_on_upload:
            self.latest = "client-v1.0.205"

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        match args[:2]:
            case ("api", "--include"):
                return f"HTTP/2 200\n\n{self.latest}\n"
            case ("api", "--paginate") if args[2].endswith("/assets"):
                return self.asset_rows()
            case ("api", "--paginate"):
                return "" if self.draft is None else f"42\t{str(self.draft).lower()}\n"
            case ("api", "--method"):
                assert args == _DELETE_ASSET
                del self.assets["Meiva_1.0.204.app.tar.gz"]
            case ("release", "create"):
                assert {"--draft", "--verify-tag"} <= set(args)
                self.draft = True
            case ("release", "upload"):
                self.upload(args[3:])
            case ("release", "edit"):
                assert args == ("release", "edit", _TAG, "--draft=false", "--latest")
            case _:
                raise AssertionError(args)
        return ""


@pytest.mark.parametrize("state", ["new", "partial draft", "starter"])
def test_only_a_verified_draft_can_become_latest(staged_files: Path, state: str) -> None:
    gh = _GitHub(draft=True if state != "new" else None)
    if state == "partial draft":
        dmg = staged_files / "Meiva_1.0.204_universal.dmg"
        gh.assets[dmg.name] = (98, "uploaded", asset_digest(dmg))
    if state == "starter":
        gh.assets["Meiva_1.0.204.app.tar.gz"] = (99, "starter", "")

    publish_client_release(
        gh, version="1.0.204", directory=staged_files, latest=_LATEST, notes="release"
    )

    assert gh.calls[-1][:2] == ("release", "edit")
    [upload] = [call for call in gh.calls if call[:2] == ("release", "upload")]
    assert len(upload[3:]) == 6 - (state == "partial draft")
    if state == "starter":
        assert gh.calls.index(_DELETE_ASSET) < gh.calls.index(upload)


@pytest.mark.parametrize("draft", [False, True])
def test_refuses_published_release_even_with_a_starter(staged_files: Path, draft: bool) -> None:
    gh = _GitHub(draft=draft)
    gh.publish_after_listing = draft
    gh.assets["Meiva_1.0.204.app.tar.gz"] = (99, "starter", "")

    with pytest.raises(ValueError, match="refusing to modify published release"):
        publish_client_release(
            gh, version="1.0.204", directory=staged_files, latest=_LATEST, notes="release"
        )

    assert all(call[:2] in {("api", "--include"), ("api", "--paginate")} for call in gh.calls)


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
        publish_client_release(
            gh, version="1.0.204", directory=staged_files, latest=_LATEST, notes="release"
        )

    assert all(call[:2] in {("api", "--include"), ("api", "--paginate")} for call in gh.calls)


@pytest.mark.parametrize("anomaly", ["bad digest", "changed Latest"])
def test_checks_the_uploaded_draft_again_before_publication(
    staged_files: Path, anomaly: str
) -> None:
    gh = _GitHub()
    gh.corrupt_uploads = anomaly == "bad digest"
    gh.move_latest_on_upload = anomaly == "changed Latest"

    with pytest.raises(ValueError):
        publish_client_release(
            gh, version="1.0.204", directory=staged_files, latest=_LATEST, notes="release"
        )

    assert all(call[:2] != ("release", "edit") for call in gh.calls)
