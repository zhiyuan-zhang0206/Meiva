from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from scripts.host_ops.release.publish_feed import publish_feed


@pytest.mark.parametrize(
    ("tag", "names", "stale"),
    [
        (
            "app-latest",
            (
                "Meiva_1.0.2_universal.dmg",
                "Meiva.app.tar.gz",
                "Meiva.app.tar.gz.sig",
                "latest.json",
            ),
            ("Meiva_1.0.1_universal.dmg", "app-universal-release.apk", "junit-e2e-shard-1.xml"),
        ),
        (
            "android-latest",
            ("meiva-app-1.0.2.apk", "android-latest.json"),
            ("meiva-app-1.0.1.apk",),
        ),
    ],
)
@pytest.mark.parametrize("failure", ["none", "upload", "missing", "wrong-digest"])
def test_feed_prunes_only_after_verified_upload(
    tmp_path: Path, tag: str, names: tuple[str, ...], stale: tuple[str, ...], failure: str
) -> None:
    files = tuple(tmp_path / name for name in names)
    for file in files:
        _ = file.write_bytes(b"release asset")
    digest = "sha256:" + hashlib.sha256(b"release asset").hexdigest()
    calls: list[tuple[str, ...]] = []

    def gh(*args: str) -> str:
        calls.append(args)
        match args[:2]:
            case ("release", "view"):
                return "123\n"
            case ("release", "upload"):
                if failure == "upload" and args[3] == str(files[-1]):
                    raise subprocess.CalledProcessError(1, args)
                return ""
            case ("api", "--paginate"):
                visible = names[:-1] if failure == "missing" else names
                rows = [
                    (name, "sha256:wrong" if failure == "wrong-digest" else digest)
                    for name in visible
                ]
                rows.extend((name, "sha256:stale") for name in stale)
                return "\n".join(f"{name}\t{value}" for name, value in rows)
            case ("release", "edit" | "delete-asset"):
                return ""
            case _:
                pytest.fail(f"unexpected GitHub command {args}")

    if failure == "none":
        publish_feed(gh, tag=tag, files=files, title="Version 1.0.2", notes="Build metadata")
        assert calls[-1] == (
            "release",
            "edit",
            tag,
            "--prerelease",
            "--latest=false",
            "--title",
            "Version 1.0.2",
            "--notes",
            "Build metadata",
        )
        assert [call[3] for call in calls if call[:2] == ("release", "delete-asset")] == sorted(
            stale
        )
    else:
        with pytest.raises((subprocess.CalledProcessError, ValueError)):
            publish_feed(gh, tag=tag, files=files, title="Version 1.0.2", notes="Build metadata")
        assert all(
            call[:2] not in {("release", "delete-asset"), ("release", "edit")} for call in calls
        )

    uploads = [command for command in calls if command[:2] == ("release", "upload")]
    assert uploads == [
        ("release", "upload", tag, *(str(path) for path in files[:-1]), "--clobber"),
        ("release", "upload", tag, str(files[-1]), "--clobber"),
    ]
