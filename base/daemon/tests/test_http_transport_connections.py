from __future__ import annotations

import asyncio
import gzip
import io
import os
import threading
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from base.daemon.http_transport import STREAM_CHUNK_BYTES, FileBody, KeepAlive, start_daemon_http

type Reply = tuple[int, Mapping[str, str], bytes]


def _request(
    path: str,
    *,
    method: str = "POST",
    body: bytes = b"",
    extra: str = "",
    version: str = "HTTP/1.1",
) -> bytes:
    head = f"{method} {path} {version}\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n{extra}"
    return head.encode() + b"\r\n" + body


async def _read_reply(reader: asyncio.StreamReader) -> Reply:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
    body = await asyncio.wait_for(reader.readexactly(int(headers["content-length"])), timeout=5)
    return status, headers, body


async def _at_eof(reader: asyncio.StreamReader) -> bool:
    return await asyncio.wait_for(reader.read(1), timeout=5) == b""


_REUSE = KeepAlive()


@asynccontextmanager
async def _serving(
    routes: dict[tuple[str, str], object],
    *,
    keep_alive: KeepAlive | None = _REUSE,
    gzip_routes: frozenset[tuple[str, str]] = frozenset(),
) -> AsyncGenerator[tuple[asyncio.Server, int]]:
    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        health_response=lambda: (200, b"{}"),
        extra_routes=routes,  # pyright: ignore[reportArgumentType]
        keep_alive=keep_alive,
        gzip_routes=gzip_routes,
    )
    async with server:
        yield server, server.sockets[0].getsockname()[1]


async def _echo(body: bytes) -> tuple[int, bytes, str]:
    return 200, b"echo:" + body, "text/plain"


@pytest.mark.asyncio
async def test_a_file_body_streams_in_chunks_and_its_handle_is_closed(tmp_path: Path) -> None:
    payload = bytes(range(256)) * ((STREAM_CHUNK_BYTES * 5 + 123) // 256 + 1)
    source = tmp_path / "big.bin"
    _ = source.write_bytes(payload)
    handles: list[io.BufferedReader] = []

    async def file_route(_body: bytes) -> tuple[int, FileBody, str, dict[str, str]]:
        handle = source.open("rb")
        _ = handle.seek(7)
        handles.append(handle)
        return 206, FileBody(handle, len(payload) - 7), "application/octet-stream", {"X-A": "1"}

    async with _serving({("POST", "/file"): file_route, ("POST", "/echo"): _echo}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/file"))
        status, headers, body = await _read_reply(reader)
        assert (status, headers["x-a"], headers["connection"]) == (206, "1", "keep-alive")
        assert headers["content-length"] == str(len(payload) - 7)
        assert body == payload[7:]
        writer.write(_request("/echo", body=b"next"))
        assert (await _read_reply(reader))[2] == b"echo:next"
        assert handles[0].closed
        writer.close()


@pytest.mark.asyncio
async def test_the_first_chunk_is_sent_before_the_rest_of_the_file_is_read() -> None:
    release = threading.Event()
    total = STREAM_CHUNK_BYTES * 3

    class _SlowTail(io.BytesIO):
        reads = 0

        def read(self, size: int | None = -1, /) -> bytes:
            type(self).reads += 1
            if type(self).reads > 1:
                assert release.wait(timeout=10)
            return super().read(size)

    async def route(_body: bytes) -> tuple[int, FileBody, str]:
        return 200, FileBody(_SlowTail(b"x" * total), total), "application/octet-stream"  # pyright: ignore[reportArgumentType]

    async with _serving({("POST", "/file"): route}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/file"))
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert f"Content-Length: {total}".encode() in head
        first = await asyncio.wait_for(reader.readexactly(STREAM_CHUNK_BYTES), timeout=5)
        assert first == b"x" * STREAM_CHUNK_BYTES
        assert not release.is_set()
        release.set()
        rest = await asyncio.wait_for(reader.readexactly(total - STREAM_CHUNK_BYTES), timeout=5)
        assert rest == b"x" * (total - STREAM_CHUNK_BYTES)
        writer.close()


@pytest.mark.asyncio
async def test_a_file_shorter_than_its_length_ends_the_connection() -> None:

    async def route(_body: bytes) -> tuple[int, FileBody, str]:
        return 200, FileBody(io.BytesIO(b"abc"), 10), "text/plain"  # pyright: ignore[reportArgumentType]

    async with _serving({("POST", "/file"): route}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/file"))
        _ = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert await asyncio.wait_for(reader.read(), timeout=5) == b"abc"
        writer.close()


@pytest.mark.asyncio
async def test_a_peer_that_disconnects_mid_stream_releases_the_file(tmp_path: Path) -> None:
    source = tmp_path / "big.bin"
    _ = source.write_bytes(b"y" * (16 * 1024 * 1024))
    opened: list[io.BufferedReader] = []

    async def route(_body: bytes) -> tuple[int, FileBody, str]:
        handle = source.open("rb")
        opened.append(handle)
        return 200, FileBody(handle, source.stat().st_size), "application/octet-stream"

    async with _serving({("POST", "/file"): route}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/file"))
        _ = await reader.readexactly(1024)
        writer.transport.abort()
        for _ in range(100):
            if opened[0].closed:
                break
            await asyncio.sleep(0.05)
        assert opened[0].closed


@pytest.mark.asyncio
async def test_one_connection_serves_many_requests_including_a_pipelined_pair() -> None:

    async def boom(_body: bytes) -> tuple[int, bytes, str]:
        raise RuntimeError("route failed")

    async with _serving({("POST", "/echo"): _echo, ("POST", "/boom"): boom}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for i in range(5):
            writer.write(_request("/echo", body=str(i).encode()))
            assert (await _read_reply(reader))[2] == f"echo:{i}".encode()
        writer.write(_request("/echo", body=b"a") + _request("/echo", body=b"b"))
        assert (await _read_reply(reader))[2] == b"echo:a"
        assert (await _read_reply(reader))[2] == b"echo:b"
        writer.write(_request("/boom"))
        status, headers, _ = await _read_reply(reader)
        assert (status, headers["connection"]) == (500, "close")
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
async def test_concurrent_connections_do_not_share_state() -> None:
    async with _serving({("POST", "/echo"): _echo}) as (_, port):

        async def client(name: int) -> list[bytes]:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            out: list[bytes] = []
            for i in range(10):
                writer.write(_request("/echo", body=f"{name}.{i}".encode()))
                out.append((await _read_reply(reader))[2])
                await asyncio.sleep(0)
            writer.close()
            return out

        results = await asyncio.gather(*(client(n) for n in range(12)))
    for name, out in enumerate(results):
        assert out == [f"echo:{name}.{i}".encode() for i in range(10)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "version", "keep_alive"),
    [
        ("Connection: close\r\n", "HTTP/1.1", KeepAlive()),
        ("Connection: Keep-Alive, Close\r\n", "HTTP/1.1", KeepAlive()),
        ("", "HTTP/1.0", KeepAlive()),
        ("", "HTTP/1.1", None),
    ],
)
async def test_connections_that_must_end_are_announced_and_closed(
    extra: str, version: str, keep_alive: KeepAlive | None
) -> None:
    async with _serving({("POST", "/echo"): _echo}, keep_alive=keep_alive) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/echo", extra=extra, version=version))
        _, headers, _ = await _read_reply(reader)
        assert headers["connection"] == "close"
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
async def test_the_request_budget_ends_the_connection_on_its_last_request() -> None:
    async with _serving({("POST", "/echo"): _echo}, keep_alive=KeepAlive(max_requests=2)) as (
        _,
        port,
    ):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/echo"))
        assert (await _read_reply(reader))[1]["connection"] == "keep-alive"
        writer.write(_request("/echo"))
        assert (await _read_reply(reader))[1]["connection"] == "close"
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
async def test_an_idle_connection_is_closed_after_the_idle_timeout() -> None:
    async with _serving({("POST", "/echo"): _echo}, keep_alive=KeepAlive(idle_seconds=0.3)) as (
        _,
        port,
    ):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/echo"))
        _ = await _read_reply(reader)
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
async def test_closing_the_server_ends_idle_connections_promptly() -> None:
    async with _serving({("POST", "/echo"): _echo}, keep_alive=KeepAlive(idle_seconds=60)) as (
        server,
        port,
    ):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/echo"))
        _ = await _read_reply(reader)
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
async def test_a_response_that_left_a_request_body_unread_closes_the_connection() -> None:
    async with _serving({("POST", "/echo"): _echo}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/healthz", method="GET"))
        status, headers, _ = await _read_reply(reader)
        assert (status, headers["connection"]) == (200, "keep-alive")
        smuggled = b"GET /healthz HTTP/1.1\r\n\r\n"
        writer.write(_request("/healthz", method="GET", body=smuggled))
        status, headers, _ = await _read_reply(reader)
        assert (status, headers["connection"]) == (200, "close")
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_header",
    [
        "Transfer-Encoding: chunked\r\n",
        "Content-Length: 5\r\n",
        "X-Fold: a\nb: c\r\n",
        "Content-Length : 5\r\n",
        "Transfer-Encoding : chunked\r\n",
        "X-Folded: a\r\n Content-Length: 5\r\n",
        "X-Folded: a\r\n\tTransfer-Encoding: chunked\r\n",
        "no-colon-here\r\n",
        "Bad Name: x\r\n",
    ],
)
async def test_ambiguous_framing_is_refused_and_nothing_after_it_is_executed(
    bad_header: str,
) -> None:
    calls: list[bytes] = []

    async def route(body: bytes) -> tuple[int, bytes, str]:
        calls.append(body)
        return 200, b"ran", "text/plain"

    smuggled = _request("/route", body=b"smuggled")
    async with _serving({("POST", "/route"): route}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/route", extra=bad_header, body=b"ok") + smuggled)
        status, headers, _ = await _read_reply(reader)
        assert (status, headers["connection"]) == (400, "close")
        assert await _at_eof(reader)
        writer.close()
    assert b"smuggled" not in calls


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", ["abc", "-5", "1_0", " ", "1.5", "9" * 5000])
async def test_a_malformed_content_length_is_refused(declared: str) -> None:
    async with _serving({("POST", "/echo"): _echo}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: {declared}\r\n\r\n".encode()
        )
        status, headers, _ = await _read_reply(reader)
        assert (status, headers["connection"]) == (400, "close")
        writer.close()


@pytest.mark.asyncio
async def test_a_half_closed_client_gets_its_answer_and_the_connection_ends() -> None:
    async with _serving({("POST", "/echo"): _echo}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/echo", body=b"hi"))
        writer.write_eof()
        assert (await _read_reply(reader))[2] == b"echo:hi"
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
async def test_a_truncated_request_body_ends_the_connection() -> None:
    async with _serving({("POST", "/echo"): _echo}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nshort")
        writer.write_eof()
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
async def test_a_not_modified_answer_names_its_status_and_keeps_the_connection() -> None:
    async def route(_body: bytes) -> tuple[int, bytes, str, dict[str, str]]:
        return 304, b"", "application/octet-stream", {"ETag": '"v1"'}

    async with _serving({("POST", "/cached"): route, ("POST", "/echo"): _echo}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/cached"))
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert head.startswith(b"HTTP/1.1 304 Not Modified\r\n")
        assert b'ETag: "v1"' in head
        writer.write(_request("/echo", body=b"after"))
        assert (await _read_reply(reader))[2] == b"echo:after"
        writer.close()


@pytest.mark.asyncio
async def test_any_connection_header_naming_close_ends_the_connection() -> None:
    async with _serving({("POST", "/echo"): _echo}) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/echo", extra="Connection: keep-alive\r\nConnection: close\r\n"))
        status, headers, _ = await _read_reply(reader)
        assert (status, headers["connection"]) == (200, "close")
        assert await _at_eof(reader)
        writer.close()


@pytest.mark.asyncio
async def test_an_error_answer_never_keeps_the_connection() -> None:
    async with _serving({("POST", "/echo"): _echo}, keep_alive=KeepAlive()) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_request("/nowhere"))
        status, headers, _ = await _read_reply(reader)
        assert (status, headers["connection"]) == (404, "close")
        assert await _at_eof(reader)
        writer.close()


_JSON_TEXT = b'{"items": [' + b'{"payload": "gzip me, I repeat myself"},' * 600 + b"{}]}"


async def _json_route(_body: bytes) -> tuple[int, bytes, str]:
    return 200, _JSON_TEXT, "application/json"


async def _partial_route(_body: bytes) -> tuple[int, bytes, str]:
    return 206, _JSON_TEXT, "application/json"


async def _archive_route(_body: bytes) -> tuple[int, bytes, str]:
    return 200, _JSON_TEXT, "application/octet-stream"


async def _small_route(_body: bytes) -> tuple[int, bytes, str]:
    return 200, b'{"ok": true}', "application/json"


async def _incompressible_route(_body: bytes) -> tuple[int, bytes, str]:
    return 200, os.urandom(4096), "application/json"


async def _big_route(_body: bytes) -> tuple[int, bytes, str]:
    return 200, _JSON_TEXT * 8, "application/json"


_GZIP_ROUTES: dict[tuple[str, str], object] = {
    ("POST", "/json"): _json_route,
    ("POST", "/partial"): _partial_route,
    ("POST", "/archive"): _archive_route,
    ("POST", "/small"): _small_route,
    ("POST", "/random"): _incompressible_route,
    ("POST", "/big"): _big_route,
    ("POST", "/not-opted-in"): _json_route,
}
_OPTED_IN = frozenset(route for route in _GZIP_ROUTES if route[1] != "/not-opted-in")


async def _exchange_once(port: int, path: str, accept: str | None) -> Reply:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    extra = "" if accept is None else f"Accept-Encoding: {accept}\r\n"
    writer.write(_request(path, extra=extra))
    reply = await _read_reply(reader)
    writer.close()
    return reply


@pytest.mark.asyncio
async def test_a_json_answer_is_gzipped_for_a_client_that_accepts_it() -> None:
    async with _serving(_GZIP_ROUTES, gzip_routes=_OPTED_IN) as (_, port):
        status, headers, body = await _exchange_once(port, "/json", "gzip, deflate")
        assert status == 200
        assert headers["content-encoding"] == "gzip"
        assert headers["vary"] == "Accept-Encoding"
        assert headers["content-length"] == str(len(body))
        assert len(body) < len(_JSON_TEXT) // 4
        assert gzip.decompress(body) == _JSON_TEXT


@pytest.mark.asyncio
async def test_a_body_past_the_offload_bound_is_gzipped_too() -> None:
    async with _serving(_GZIP_ROUTES, gzip_routes=_OPTED_IN) as (_, port):
        _, headers, body = await _exchange_once(port, "/big", "gzip")
        assert headers["content-encoding"] == "gzip"
        assert gzip.decompress(body) == _JSON_TEXT * 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "accept"),
    [
        ("/json", None),
        ("/json", "identity"),
        ("/json", "gzip;q=0"),
        ("/json", "br, deflate"),
        ("/small", "gzip"),
        ("/archive", "gzip"),
        ("/partial", "gzip"),
        ("/random", "gzip"),
        ("/not-opted-in", "gzip"),
    ],
)
async def test_every_other_answer_leaves_uncompressed(path: str, accept: str | None) -> None:
    async with _serving(_GZIP_ROUTES, gzip_routes=_OPTED_IN) as (_, port):
        _, headers, body = await _exchange_once(port, path, accept)
        assert "content-encoding" not in headers
        assert headers["content-length"] == str(len(body))


@pytest.mark.asyncio
async def test_a_gzipped_answer_keeps_the_connection_reusable() -> None:
    async with _serving(_GZIP_ROUTES, gzip_routes=_OPTED_IN) as (_, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(2):
            writer.write(_request("/json", extra="Accept-Encoding: gzip\r\n"))
            _, headers, body = await _read_reply(reader)
            assert headers["connection"] == "keep-alive"
            assert gzip.decompress(body) == _JSON_TEXT
        writer.close()
