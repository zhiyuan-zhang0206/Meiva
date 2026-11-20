from datetime import UTC, datetime

import httpx
import pytest
from fastapi import HTTPException

from base.agents.to_user import ToUserReadRequest, WorkspaceFileRequest, WorkspaceFileResponse
from base.config import settings
from gateway.agents.to_user.document import (
    fetch_resource,
    fetch_workspace_json,
    open_workspace_file_raw,
    relay_body,
    resource_request,
    resource_response,
)


def test_html_downloads_without_scripts() -> None:
    html = resource_response(b"<script>alert(1)</script>", 'test".html')
    assert html.headers["content-type"].startswith("text/html")
    assert html.headers["content-disposition"].startswith("attachment;")
    assert "%22" in html.headers["content-disposition"]
    policy = html.headers["content-security-policy"]
    assert policy.startswith("sandbox;")
    assert "allow-same-origin" not in policy
    assert "default-src 'none'" in policy
    assert "allow-scripts" not in policy
    assert html.headers["cache-control"] == "no-store"
    assert html.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "name",
    [
        "plot.png",
        "plot.svg",
        "voice.mp3",
        "clip.mp4",
        "report.pdf",
        "notes.md",
        "log.txt",
        "data.json",
        "table.csv",
        "feed.xml",
    ],
)
def test_passive_files_render_inline_without_scripts(name: str) -> None:
    response = resource_response(b"payload", name)
    assert response.headers["content-disposition"].startswith("inline;")
    policy = response.headers["content-security-policy"]
    assert policy.startswith("sandbox;")
    assert "allow-scripts" not in policy
    assert "default-src 'none'" in policy


@pytest.mark.parametrize(
    "name",
    ["tool.sh", "bundle.js", "archive.zip", "setup.exe", "report.docx", "book.xlsx", "blob.bin"],
)
def test_active_archives_and_unknown_files_stay_attachments(name: str) -> None:
    response = resource_response(b"payload", name)
    assert response.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("files", [[""], ["missing.md"] * (settings.to_user.max_chain_depth + 1)])
def test_malformed_names_and_unbounded_chains_are_rejected(files: list[str]) -> None:
    with pytest.raises(HTTPException) as error:
        resource_request(1, files, max_depth=settings.to_user.max_chain_depth)
    assert error.value.status_code == 422


# meiva:note
# The gateway bounds the shape of a name and nothing else: a readable path makes any path well
# formed, so whether the document references it is the owning machine's answer — an unreferenced
# path comes back `gone` rather than being refused here.
def test_a_path_the_document_may_not_reference_still_travels() -> None:
    query = resource_request(1, ["/etc/passwd"], max_depth=settings.to_user.max_chain_depth)
    assert query.files == ["/etc/passwd"]


@pytest.mark.asyncio
async def test_binary_response_and_auth_are_preserved() -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test"
        assert request.url.path == "/to-user/resource"
        return httpx.Response(200, content=b"\x00\xffbinary")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await fetch_resource(
            client,
            "http://runner/to-user/resource",
            ToUserReadRequest(agent_id=1),
            {"Authorization": "Bearer test"},
            limit=settings.to_user.max_resource_bytes,
        )
    assert result == b"\x00\xffbinary"


@pytest.mark.asyncio
async def _oversize_stream():
    chunk = b"x" * (4 * 1024 * 1024)
    remaining = settings.to_user.max_resource_bytes + 1
    while remaining > 0:
        yield chunk[: min(len(chunk), remaining)]
        remaining -= len(chunk)


async def test_missing_file_and_oversize_fail_explicitly() -> None:
    for status, content, expected in [
        (404, b"gone", 404),
        (200, _oversize_stream(), 413),
    ]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, status=status, content=content: httpx.Response(status, content=content)
            )
        ) as client:
            with pytest.raises(HTTPException) as error:
                await fetch_resource(
                    client,
                    "http://runner/to-user/resource",
                    ToUserReadRequest(agent_id=1),
                    {},
                    limit=settings.to_user.max_resource_bytes,
                )
            assert error.value.status_code == expected


@pytest.mark.asyncio
async def test_workspace_file_proxy_content_and_raw() -> None:
    expected_payload = WorkspaceFileResponse(
        path="src/main.py",
        name="main.py",
        extension=".py",
        content="print(42)\n",
        size=10,
        binary=False,
        modified_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
    )

    def serve(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test"
        assert request.url.path == "/to-user/workspace-file"
        body = WorkspaceFileRequest.model_validate_json(request.content)
        if body.path == "missing.py":
            return httpx.Response(404, text="File 'missing.py' not found")
        if body.mode == "content":
            return httpx.Response(
                200,
                content=expected_payload.model_dump_json().encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"print(42)\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        res = await fetch_workspace_json(
            client,
            "http://runner/to-user/workspace-file",
            WorkspaceFileRequest(agent_id=1, path="src/main.py", mode="content"),
            {"Authorization": "Bearer test"},
            WorkspaceFileResponse,
        )
        assert res == expected_payload

        upstream = await open_workspace_file_raw(
            client,
            "http://runner/to-user/workspace-file",
            WorkspaceFileRequest(agent_id=1, path="src/main.py", mode="raw"),
            {"Authorization": "Bearer test"},
            limit=settings.to_user.max_resource_bytes,
        )
        relayed = b"".join(
            [
                chunk
                async for chunk in relay_body(upstream, limit=settings.to_user.max_resource_bytes)
            ]
        )
        assert relayed == b"print(42)\n"

        with pytest.raises(HTTPException) as exc_info:
            await fetch_workspace_json(
                client,
                "http://runner/to-user/workspace-file",
                WorkspaceFileRequest(agent_id=1, path="missing.py", mode="content"),
                {"Authorization": "Bearer test"},
                WorkspaceFileResponse,
            )
        assert exc_info.value.status_code == 404
        assert "missing.py" in str(exc_info.value.detail)


# meiva:note
# A runner that predates a request field refuses any field it does not know, so the forwarded
# body carries only the fields this read sets.
@pytest.mark.asyncio
async def test_forwarded_reads_carry_only_the_fields_they_set() -> None:
    sent: list[set[str]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        sent.append(WorkspaceFileRequest.model_validate_json(request.content).model_fields_set)
        return httpx.Response(200, content=b"x", headers={"content-length": "1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        upstream = await open_workspace_file_raw(
            client,
            "http://runner/to-user/workspace-file",
            WorkspaceFileRequest(agent_id=1, path="clip.mp4", mode="raw"),
            {},
            limit=settings.to_user.max_resource_bytes,
        )
        await upstream.aclose()
    assert sent == [{"agent_id", "path", "mode"}]


@pytest.mark.asyncio
async def test_a_whole_forwarded_file_over_the_limit_is_refused_before_relaying() -> None:
    def serve(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"0123456789")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(HTTPException) as error:
            await open_workspace_file_raw(
                client,
                "http://runner/to-user/workspace-file",
                WorkspaceFileRequest(agent_id=1, path="clip.mp4", mode="raw"),
                {},
                limit=4,
            )
    assert error.value.status_code == 413


# meiva:note
# A runner that predates `byte_range` refuses the field; the media still plays, as a whole file.
@pytest.mark.asyncio
async def test_a_runner_without_ranges_still_serves_the_whole_file() -> None:
    sent: list[str | None] = []

    def serve(request: httpx.Request) -> httpx.Response:
        query = WorkspaceFileRequest.model_validate_json(request.content)
        sent.append(query.byte_range)
        if query.byte_range is not None:
            return httpx.Response(422, text="Invalid workspace file request")
        return httpx.Response(200, content=b"whole")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        upstream = await open_workspace_file_raw(
            client,
            "http://runner/to-user/workspace-file",
            WorkspaceFileRequest(agent_id=1, path="clip.mp4", mode="raw", byte_range="bytes=0-1"),
            {},
            limit=settings.to_user.max_resource_bytes,
        )
        assert upstream.status_code == 200
        assert await upstream.aread() == b"whole"
    assert sent == ["bytes=0-1", None]
