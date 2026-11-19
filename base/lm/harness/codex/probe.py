from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from base.lm.harness.codex.auth import build_env
from base.lm.harness.codex.config import CodexConfig
from base.lm.harness.codex.frames import JsonObject, JsonRpcError
from base.lm.harness.codex.requests import INITIALIZED, initialize_request
from base.lm.harness.codex.transport import StdioAppServer

_PROBE_STDERR_LIMIT_BYTES = 64 * 1024


class CodexAppServerProbe(NamedTuple):
    models_page: JsonObject | None
    account_result: JsonObject | None
    account_error: str | None
    account_error_code: int | None
    stderr_text: str


async def _drain_stderr(stream: asyncio.StreamReader | None, *, limit: int) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if total < limit:
            remaining = limit - total
            chunks.append(chunk[:remaining])
            total += min(len(chunk), remaining)
    return b"".join(chunks).decode("utf-8", errors="replace")


# meiva:note
# Probes Codex `account/read` (`refreshToken: False`) and `model/list` inside one app-server
# session while capturing stderr so invalidated refresh tokens are caught without rotating valid
# credentials or spending inference quota.
# meiva:link base/agents/docs/harness/availability.md
async def probe_account_and_models(
    binary: str, *, cwd: Path, environ: Mapping[str, str], timeout: float
) -> CodexAppServerProbe:
    config = CodexConfig(cwd=cwd)
    server = StdioAppServer(
        config.command(binary),
        cwd=config.cwd,
        environ=build_env(environ),
        stderr=subprocess.PIPE,
    )
    models_page: JsonObject | None = None
    account_result: JsonObject | None = None
    account_error: str | None = None
    account_error_code: int | None = None
    stderr_text = ""
    try:
        async with asyncio.timeout(timeout):
            await server.start()
            async with asyncio.TaskGroup() as tg:
                stderr_task = tg.create_task(
                    _drain_stderr(server.stderr, limit=_PROBE_STDERR_LIMIT_BYTES)
                )
                try:
                    _ = await server.request(*initialize_request())
                    await server.notify(INITIALIZED)
                    try:
                        account_result = await server.request(
                            "account/read", {"refreshToken": False}
                        )
                    except JsonRpcError as exc:
                        account_error = exc.message
                        account_error_code = exc.code
                    account_missing = (
                        account_result is not None
                        and account_result.get("account") is None
                        and account_result.get("requiresOpenaiAuth") is True
                    )
                    if not account_missing and (
                        account_error is None or account_error_code == -32601
                    ):
                        models_page = await server.request("model/list", {})
                finally:
                    await server.close()
            stderr_text = stderr_task.result()
    finally:
        await server.close()
    return CodexAppServerProbe(
        models_page=models_page,
        account_result=account_result,
        account_error=account_error,
        account_error_code=account_error_code,
        stderr_text=stderr_text,
    )
