# meiva:note
# The RPC client as a gateway-profile process builds it from its unit configuration.
#
# Runs in a child process whose environment is a real gateway profile and whose unit `.env`
# carries the timeout and retry settings, so `RpcClient.system()` reads real configuration
# without any settings attribute being replaced. Cases: `config` prints what the system client
# was built with and presents; `proxy` dispatches to the loopback server named by `argv[2]` with
# a dead proxy in the environment.

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from base.cluster.machine import gateway_auth_headers
from base.db import Database, DbConfig
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from ops.cluster.rpc import RpcClient, dispatch_to_machine, dispatch_to_url


def config(ops_url: str) -> None:
    client = RpcClient.system()
    asyncio.run(client.dispatch_to_url("runner-a", "status_probe", {}, ops_url=ops_url, retries=0))
    sys.stdout.write(
        json.dumps(
            {
                "timeout_seconds": client.config.timeout_seconds,
                "max_retries": client.config.max_retries,
                "authorization": gateway_auth_headers(client.access())["Authorization"],
            }
        )
    )


def proxy(ops_url: str) -> None:
    client = RpcClient.system()
    result = asyncio.run(
        client.dispatch_to_url("runner-a", "status_probe", {}, ops_url=ops_url, retries=0)
    )
    sys.stdout.write(json.dumps(result))


def unknown_kind(case: str, ops_url: str) -> None:
    version = CodeVersion(LoadedCommit(Path(__file__).resolve().parents[2], None))
    database = Database(
        DbConfig(
            db_url="postgresql://denied@127.0.0.1:1/denied",
            db_sslmode="disable",
            db_pool_min_size=1,
            db_pool_max_size=1,
            pgbouncer_enabled=False,
            db_authority_refusal="this probe has no database authority",
        ),
        gate=ProcessDbGate(version=version.get, process="invalid-rpc-input"),
    )
    try:
        if case == "unknown-machine":
            asyncio.run(dispatch_to_machine(database, "runner-a", "no-such-op", {}))
        else:
            asyncio.run(dispatch_to_url("runner-a", "no-such-op", {}, ops_url=ops_url))
    except ValueError as exc:
        assert str(exc) == "unknown op kind: 'no-such-op'", str(exc)
        sys.stdout.write(json.dumps({"error": str(exc)}))
    else:
        raise AssertionError("unknown operation was accepted")


if __name__ == "__main__":
    case, url = sys.argv[1], sys.argv[2]
    if case == "config":
        config(url)
    elif case == "proxy":
        proxy(url)
    elif case in {"unknown-machine", "unknown-url"}:
        unknown_kind(case, url)
    else:
        raise SystemExit(f"unknown case {case!r}")
