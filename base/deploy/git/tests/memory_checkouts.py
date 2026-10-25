import contextlib
import subprocess
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

import pytest

from base.deploy.git.memory_repo import init_local_repo, run_git


def commit_note(root: Path, body: str) -> str:
    (root / "note.md").write_text(f"---\ntype: Memory\ntitle: Note\n---\n\n{body}\n")
    run_git("add", "note.md", cwd=root)
    run_git("-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-qm", body, cwd=root)
    return run_git("rev-parse", "HEAD", cwd=root)


@pytest.fixture
def memory_checkout(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    root = tmp_path / "checkout"
    remote = tmp_path / "remote.git"
    init_local_repo("main", source)
    commit_note(source, "before refresh")
    run_git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    run_git("remote", "add", "origin", str(remote), cwd=source)
    run_git("clone", str(remote), str(root), cwd=tmp_path)
    return source, root


def make_bundle(source: Path) -> tuple[str, bytes]:
    # meiva:note
    # Create the actual Git wire artifact served by the gateway pool route.
    # meiva:link base/deploy/git/tests/docs/memory-repo-tests.meiva.okf.md
    init_local_repo("main", source)
    (source / "MEMORY.md").write_text("# shared index\n")
    (source / "health").mkdir()
    (source / "health" / "a.md").write_text("note")
    run_git("add", "-A", cwd=source)
    run_git("commit", "-qm", "shared pool", cwd=source)
    head = run_git("rev-parse", "HEAD", cwd=source)
    bundle = source.parent / "pool.bundle"
    run_git("bundle", "create", str(bundle), "HEAD", cwd=source)
    return head, bundle.read_bytes()


@contextlib.contextmanager
def git_http_remote(remote: Path, *, received: Event, release: Event) -> Iterator[str]:
    # meiva:note
    # Expose a local bare repository through Git's own CGI backend, holding discovery.
    # meiva:link base/deploy/git/tests/docs/memory-repo-tests.meiva.okf.md

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.set()
            if not release.wait(5):
                self.send_error(504)
                return
            self._respond(b"")

        def do_POST(self) -> None:
            self.connection.settimeout(5)
            self._respond(self.rfile.read(int(self.headers["Content-Length"])))

        def _respond(self, body: bytes) -> None:
            request = urlsplit(self.path)
            result = subprocess.run(
                ["git", "http-backend"],
                env={
                    "GIT_PROJECT_ROOT": str(remote.parent),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "PATH_INFO": request.path,
                    "QUERY_STRING": request.query,
                    "SERVER_PROTOCOL": "HTTP/1.1",
                },
                input=body,
                capture_output=True,
                check=True,
                timeout=10,
            )
            headers, _, body = result.stdout.partition(b"\r\n\r\n")
            self.send_response(200)
            for header in headers.decode().splitlines():
                name, _, value = header.partition(":")
                self.send_header(name, value.strip())
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{remote.name}"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(5)
        assert not worker.is_alive(), "Git HTTP listener did not stop"
