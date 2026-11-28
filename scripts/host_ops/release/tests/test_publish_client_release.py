from pathlib import Path

import pytest

from scripts.host_ops.release.github_releases import asset_digest
from scripts.host_ops.release.publish_client_release import publish_client_release


@pytest.mark.parametrize(
    "state", ["new", "partial draft", "published", "bad digest", "changed Latest"]
)
def test_only_a_verified_draft_can_become_latest(tmp_path: Path, state: str) -> None:
    names = (
        "Meiva_1.0.204_universal.dmg",
        "Meiva_1.0.204.app.tar.gz",
        "Meiva_1.0.204.app.tar.gz.sig",
        "latest.json",
        "meiva-app-1.0.203.apk",
        "android-latest.json",
    )
    files = [tmp_path / name for name in names]
    for file in files:
        _ = file.write_text(file.name)
    uploaded = {files[0].name: asset_digest(files[0])} if state == "partial draft" else {}
    calls: list[tuple[str, ...]] = []
    tag = "client-v1.0.204"
    latest = "client-v1.0.203"

    def release_rows() -> str:
        if state == "published":
            return "42\tfalse\n"
        if state == "partial draft" or any(call[:2] == ("release", "create") for call in calls):
            return "42\ttrue\n"
        return ""

    def gh(*args: str) -> str:
        calls.append(args)
        match args[:2]:
            case ("api", "--include"):
                holder = "client-v1.0.205" if state == "changed Latest" and uploaded else latest
                return f"HTTP/2 200\n\n{holder}\n"
            case ("api", "--paginate") if args[2].endswith("/assets"):
                return "\n".join(f"{name}\t{digest}" for name, digest in uploaded.items())
            case ("api", "--paginate"):
                return release_rows()
            case ("release", "create"):
                assert {"--draft", "--verify-tag"} <= set(args)
            case ("release", "upload"):
                for item in args[3:]:
                    path = Path(item)
                    uploaded[path.name] = asset_digest(path) if state != "bad digest" else "corrupt"
            case ("release", "edit"):
                assert args == ("release", "edit", tag, "--draft=false", "--latest")
            case _:
                raise AssertionError(args)
        return ""

    if state in ("new", "partial draft"):
        publish_client_release(
            gh, version="1.0.204", directory=tmp_path, latest=latest, notes="release"
        )
        assert calls[-1][:2] == ("release", "edit")
        [upload] = [call for call in calls if call[:2] == ("release", "upload")]
        assert len(upload[3:]) == len(names) - (state == "partial draft")
    else:
        with pytest.raises(ValueError):
            publish_client_release(
                gh, version="1.0.204", directory=tmp_path, latest=latest, notes="release"
            )
        assert all(call[:2] != ("release", "edit") for call in calls)
