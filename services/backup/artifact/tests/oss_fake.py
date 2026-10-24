# meiva:note
# A loopback OSS server for the off-site publisher tests.
#
# Not a test module: pytest never collects this file. The server is a real
# listener on port 0 that the real `oss2` SDK talks to over HTTP, and it behaves
# as OSS does for the verbs a publish uses: per-part Content-MD5 verification,
# the forbid-overwrite precondition on CompleteMultipartUpload, the deterministic
# multipart ETag chain (uppercase hex, as OSS answers), and user metadata.

from __future__ import annotations

import base64
import hashlib
import re
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

BUCKET = "backups"


# meiva:note
# OSS names objects and parts by MD5, a wire-format digest and not a secret, so the
# S324 suppression is the format's, not a shortcut.
def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324


# meiva:note
# The ETag OSS gives a multipart object whose parts are `parts`.
def chain_etag(parts: Sequence[bytes]) -> str:
    token = "".join(md5_hex(part).upper() for part in parts)
    return f"{md5_hex(token.encode()).upper()}-{len(parts)}"


# meiva:note
# The state of one loopback bucket and the switches that break it on purpose.
# `calls` holds the (verb, publisher headers) of every call that carried headers, in
# order.
@dataclass
class FakeOss:
    files: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    pending: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    aborted: list[str] = field(default_factory=list[str])
    calls: list[tuple[str, dict[str, str]]] = field(
        default_factory=list[tuple[str, dict[str, str]]]
    )
    corrupt_part_etags: bool = False
    corrupt_complete_etag: bool = False
    fail_init: tuple[int, str] | None = None
    endpoint: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_id: int = 1

    # meiva:note
    # An object a previous run completed: `parts` joined, ETag the chain.
    def seed(
        self, key: str, parts: Sequence[bytes], metadata: Mapping[str, str] | None = None
    ) -> None:
        self.files[key] = {
            "etag": chain_etag(parts),
            "size": sum(len(part) for part in parts),
            "headers": {f"x-oss-meta-{k}": v for k, v in (metadata or {}).items()},
            "data": b"".join(parts),
        }


def _meta(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if k.lower().startswith("x-oss-meta-")}


# meiva:note
# The headers the publisher chose (not the transport's own), lowercased.
def _publisher_headers(headers: Mapping[str, str]) -> dict[str, str]:
    lowered = {k.lower(): v for k, v in headers.items()}
    return {
        k: v
        for k, v in lowered.items()
        if k in ("content-md5", "x-oss-forbid-overwrite") or k.startswith("x-oss-meta-")
    }


def _handler(state: FakeOss) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return None

        def _target(self) -> tuple[str, dict[str, list[str]]]:
            parts = urlsplit(self.path)
            bucket, _, key = unquote(parts.path).lstrip("/").partition("/")
            assert bucket == BUCKET, bucket
            return key, parse_qs(parts.query, keep_blank_values=True)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length", "0")))

        def _send(
            self,
            status: int,
            body: bytes = b"",
            headers: Mapping[str, str] | None = None,
            length: int | None = None,
        ) -> None:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("x-oss-request-id", "loopback")
            self.send_header("Content-Length", str(len(body) if length is None else length))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _error(self, status: int, code: str) -> None:
            xml = (
                f"<?xml version='1.0'?><Error><Code>{code}</Code><Message>{code}</Message>"
                "<RequestId>loopback</RequestId><HostId>loopback</HostId></Error>"
            )
            self._send(status, xml.encode(), {"Content-Type": "application/xml"})

        def do_POST(self) -> None:
            key, query = self._target()
            body = self._body()
            with state.lock:
                if "uploads" in query:
                    self._init(key)
                else:
                    self._complete(key, query["uploadId"][0], body)

        def _init(self, key: str) -> None:
            state.calls.append(("init", _publisher_headers(dict(self.headers.items()))))
            if state.fail_init is not None:
                self._error(*state.fail_init)
                return
            upload_id = f"up{state.next_id}"
            state.next_id += 1
            state.pending[upload_id] = {
                "key": key,
                "headers": _meta(dict(self.headers.items())),
                "parts": {},
            }
            xml = (
                "<?xml version='1.0'?><InitiateMultipartUploadResult>"
                f"<Bucket>{BUCKET}</Bucket><Key>{key}</Key><UploadId>{upload_id}</UploadId>"
                "</InitiateMultipartUploadResult>"
            )
            self._send(200, xml.encode(), {"Content-Type": "application/xml"})

        def _complete(self, key: str, upload_id: str, body: bytes) -> None:
            state.calls.append(("complete", _publisher_headers(dict(self.headers.items()))))
            if key in state.files and self.headers.get("x-oss-forbid-overwrite") == "true":
                self._error(409, "FileAlreadyExists")
                return
            numbers = [int(n) for n in re.findall(rb"<PartNumber>(\d+)</PartNumber>", body)]
            pending = state.pending.pop(upload_id)
            bodies = [pending["parts"][number] for number in numbers]
            etag = chain_etag(bodies)
            if state.corrupt_complete_etag:
                etag = etag[:-3] + "999"
            state.files[key] = {
                "etag": etag,
                "size": sum(len(part) for part in bodies),
                "headers": pending["headers"],
                "data": b"".join(bodies),
            }
            self._send(200, b"", {"ETag": f'"{etag}"'})

        def do_PUT(self) -> None:
            _key, query = self._target()
            body = self._body()
            with state.lock:
                state.calls.append(("part", _publisher_headers(dict(self.headers.items()))))
                expected = self.headers.get("Content-MD5")
                if expected != base64.b64encode(bytes.fromhex(md5_hex(body))).decode():
                    self._error(400, "InvalidDigest")
                    return
                number = int(query["partNumber"][0])
                state.pending[query["uploadId"][0]]["parts"][number] = body
                etag = "Z" * 32 if state.corrupt_part_etags else md5_hex(body).upper()
                self._send(200, b"", {"ETag": f'"{etag}"'})

        def do_HEAD(self) -> None:
            key, _query = self._target()
            with state.lock:
                if key not in state.files:
                    self._send(404)
                    return
                record = state.files[key]
                self._send(
                    200,
                    b"",
                    {**record["headers"], "ETag": f'"{record["etag"]}"'},
                    length=record["size"],
                )

        def do_DELETE(self) -> None:
            _key, query = self._target()
            with state.lock:
                upload_id = query["uploadId"][0]
                state.pending.pop(upload_id)
                state.aborted.append(upload_id)
                self._send(204)

    return Handler


# meiva:note
# A loopback OSS listener on port 0; `endpoint` is its `http://127.0.0.1:<port>` URL.
@contextmanager
def fake_oss() -> Generator[FakeOss]:
    state = FakeOss()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
