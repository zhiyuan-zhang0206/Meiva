# meiva:note
# Another machine's ops listener for the workspace reads the gateway forwards. It answers
# `POST /to-user/workspace-file` for one agent with `ops.to_user.answer_workspace_file` (the same
# answer the runner's route sends) over a real HTTP round trip, from a Meiva home and an account
# home that are not the gateway's, so a test sees what crossed the wire: the forwarded body, the
# status, the `Content-Range`, the root a path bar starts from. A request for any other agent answers 404, so a read routed to the wrong
# machine fails loudly.

import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import override

import psycopg

from base.agents.to_user import WorkspaceFileRequest, to_user_limits
from base.config import settings
from ops.to_user import WorkspaceFileError, answer_workspace_file


@contextmanager
def remote_workspace_ops_server(
    remote_home: Path, expected_agent_id: int, *, user_home: Path
) -> Generator[str]:
    limits = to_user_limits(settings.to_user)

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/to-user/workspace-file":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            req = WorkspaceFileRequest.model_validate_json(self.rfile.read(length))
            if req.agent_id != expected_agent_id:
                self.send_response(404)
                self.end_headers()
                _ = self.wfile.write(b"Wrong agent routed to remote machine")
                return
            try:
                answer = answer_workspace_file(
                    req, home=remote_home, user_home=user_home, limits=limits
                )
            except WorkspaceFileError as exc:
                self.send_response(exc.status_code)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                _ = self.wfile.write(exc.detail.encode("utf-8"))
                return
            self.send_response(answer[0])
            self.send_header("Content-Type", answer[2])
            self.send_header("Content-Length", str(len(answer[1])))
            headers: Mapping[str, str] = answer[3] if len(answer) == 4 else {}
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            _ = self.wfile.write(answer[1])

        @override
        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# meiva:note
# Point the gateway's machine lookup for `machine` at a listener.
def register_runner(conn: psycopg.Connection, machine: str, ops_url: str) -> None:
    _ = conn.execute(
        "INSERT INTO machines (name, role, gateway_url) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET gateway_url = EXCLUDED.gateway_url",
        (machine, ["agent-runner"], ops_url),
    )
    conn.commit()
