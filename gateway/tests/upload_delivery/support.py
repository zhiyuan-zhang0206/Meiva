# meiva:note
# Delivery support that runs in this process: the gateway's object endpoint on a loopback port,
# and the receiving runner's `upload-receive-v2` op.
#
# A delivered upload is a receiver's copy now — the staging directory the gateway received the
# bytes in is not the agent's workspace — so a test that copies a batch needs two things a bare
# `TestClient` does not give it: a dialable gateway address to pull from, and a runner that
# answers the receive op. Both are the real code here; only the transport is local.

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from psycopg_pool import ConnectionPool

from base.agents.upload_delivery.models import ReceiveRequest, UnitIdentity
from gateway.tests.auth.gateway_config import GatewayTestClient
from gateway.tests.support.runner_ops.ops_stub import OpsCall, OpsReply, completed, refused
from ops import upload_delivery as receiver


# meiva:note
# The client's HTTP surface on a loopback port for a caller that must dial the gateway. `pool` is
# the one a pull may use: the handler takes a connection before answering, so a receiver that
# holds its only connection across the HTTP call fails here instead of hiding a pool-exhaustion
# bug.
@contextmanager
def serving_gateway(
    client: GatewayTestClient, pool: ConnectionPool, *, timeout: float = 5.0
) -> Generator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            with pool.connection(timeout=timeout) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            result = client.get(self.path)
            self.send_response(result.status_code)
            self.send_header("Content-Length", str(len(result.content)))
            self.end_headers()
            self.wfile.write(result.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# meiva:note
# The receiver config a runner on `unit` runs with: the gateway's own quota limits and the
# delivered-directory name the cluster configured, so both writers of that path agree.
def receiver_for(client: GatewayTestClient, unit: UnitIdentity) -> receiver.ReceiverConfig:
    config = client.app.state.gateway_config
    return receiver.ReceiverConfig(
        unit=unit,
        upload_dir_name=config.upload_dir_name,
        access=client.app.state.gateway_access,
        quota_bytes=config.max_agent_upload_bytes,
        quota_files=config.max_agent_upload_files,
    )


# meiva:note
# The receiving runner's `upload-receive-v2` op: the real receive code, in this process.
#
# With `lose_first_response` the first call completes its work (the bytes are published and the
# ready marker committed) and the caller then sees the connection answer 404.
def receiver_op(
    client: GatewayTestClient,
    config: receiver.ReceiverConfig,
    *,
    lose_first_response: bool = False,
) -> Any:
    calls = 0

    def receive(call: OpsCall) -> OpsReply:
        nonlocal calls
        calls += 1
        assert call.idempotency_key is None
        assert call.kind == "upload-receive-v2"
        proof = receiver.copy_batch(
            client.app.state.db_pool,
            ReceiveRequest.model_validate(call.payload),
            config,
        ).model_dump(mode="json")
        if lose_first_response and calls == 1:
            return refused(404, "remote fsync/ready succeeded but response lost")
        return completed(proof)

    return receive
