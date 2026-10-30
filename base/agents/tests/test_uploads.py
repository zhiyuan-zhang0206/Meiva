"""base.agents.upload_delivery.paths — url <-> path mapping + traversal-safe resolution + base64.

Pure unit (no DB / no gateway). The traversal guard is security-critical: a
crafted image reference must never resolve outside the agent's upload dir.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from base.agents.messages.inbound_images import image_mime_for, parse_upload_url
from base.agents.upload_delivery import paths


class TestParseUploadUrl:
    def test_valid(self) -> None:
        assert parse_upload_url("/api/agents/7/uploads/foo.png") == (7, "foo.png")

    def test_rejects_foreign_url(self) -> None:
        assert parse_upload_url("http://evil.example/x.png") is None
        assert parse_upload_url("/api/agents/7/timeline") is None
        assert parse_upload_url("/api/agents/abc/uploads/x.png") is None

    def test_keeps_nested_name_for_resolver_to_reject(self) -> None:
        # parse does not itself sanitize; it hands the raw name to the resolver.
        assert parse_upload_url("/api/agents/7/uploads/../secret") == (7, "../secret")


class TestUploadDirectories:
    # meiva:note
    # A staging directory left group-readable is repaired before files are read or written.
    def test_staging_dir_is_owner_only(self, tmp_path: Path) -> None:
        staging = tmp_path / "uploads"
        directory = staging / "7"
        directory.mkdir(parents=True)
        directory.chmod(0o755)

        assert paths.agent_upload_dir(7, root=staging) == directory
        assert directory.stat().st_mode & 0o777 == 0o700

    def test_staging_dir_is_per_agent_below_the_root(self, tmp_path: Path) -> None:
        assert paths.agent_upload_dir(7, root=tmp_path, create=False) == tmp_path / "7"

    def test_delivered_copy_lands_in_the_agent_workspace(self, tmp_path: Path) -> None:
        assert paths.delivered_upload_dir(7, home=tmp_path, name="uploads") == (
            tmp_path / "workspaces" / "7" / "uploads"
        )

    def test_delivered_copy_honours_the_configured_name(self, tmp_path: Path) -> None:
        assert paths.delivered_upload_dir(7, home=tmp_path, name="inbox") == (
            tmp_path / "workspaces" / "7" / "inbox"
        )

    def test_delivered_copy_is_created_owner_only(self, tmp_path: Path) -> None:
        directory = paths.delivered_upload_dir(7, home=tmp_path, name="uploads")
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700


class TestResolveUploadPath:
    def test_resolves_inside_dir(self, tmp_path: Path) -> None:
        p = paths.resolve_upload_path(tmp_path, "foo.png")
        assert p == (tmp_path / "foo.png").resolve()

    def test_rejects_parent_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes"):
            paths.resolve_upload_path(tmp_path, "../../etc/passwd")

    def test_rejects_absolute(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes"):
            paths.resolve_upload_path(tmp_path, "/etc/passwd")


class TestImageMime:
    def test_known_image_suffixes(self) -> None:
        assert image_mime_for("a.png") == "image/png"
        assert image_mime_for("a.JPG") == "image/jpeg"
        assert image_mime_for("a.webp") == "image/webp"

    def test_non_image_returns_none(self) -> None:
        assert image_mime_for("a.pdf") is None
        assert image_mime_for("a.txt") is None
        assert image_mime_for("noext") is None


class TestFetchUploadB64:
    def test_fetches_and_encodes(self) -> None:
        raw = b"\x89PNG\r\n\x1a\n" + b"pixels"
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=raw)

        with httpx.Client(
            base_url="http://gw.test:8000",
            headers={"Authorization": "Bearer test-value"},
            transport=httpx.MockTransport(answer),
        ) as client:
            mime, b64 = paths.fetch_upload_b64(7, "shot.png", client=lambda: client)
        assert mime == "image/png"
        assert base64.standard_b64decode(b64) == raw
        assert str(seen[0].url) == "http://gw.test:8000/api/agents/7/uploads/shot.png"
        assert seen[0].headers["Authorization"] == "Bearer test-value"
        assert seen[0].extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 15.0
        )

    def test_non_image_raises(self) -> None:
        def unexpected(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"must not fetch {request.url}")

        with (
            httpx.Client(transport=httpx.MockTransport(unexpected)) as client,
            pytest.raises(ValueError, match="not a recognized image"),
        ):
            paths.fetch_upload_b64(7, "notes.txt", client=lambda: client)

    def test_missing_upload_raises_oserror(self) -> None:
        with (
            httpx.Client(
                base_url="http://gw.test:8000",
                transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
            ) as client,
            pytest.raises(OSError, match="404") as failure,
        ):
            paths.fetch_upload_b64(7, "gone.png", client=lambda: client)
        assert isinstance(failure.value.__cause__, httpx.HTTPStatusError)


def test_upload_url_roundtrips() -> None:
    assert parse_upload_url(paths.upload_url(3, "a.png")) == (3, "a.png")
