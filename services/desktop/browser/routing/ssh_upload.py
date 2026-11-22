# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import asyncio
import base64
import shlex
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import PurePosixPath, PureWindowsPath

from base.host.remote_hosts import RemoteHostConfig

from .remote_files import upload_name

_TIMEOUT_S = 12.0


def _command(config: RemoteHostConfig, filename: str) -> str:
    if config.os == "windows":
        filename64 = base64.b64encode(filename.encode("utf-8")).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$dir = Join-Path ([IO.Path]::GetTempPath()) ('meiva-chrome-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($dir)
try {{
    $name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{filename64}'))
    $file = Join-Path $dir $name
    [IO.File]::WriteAllBytes($file, [Convert]::FromBase64String([Console]::ReadLine()))
    [Console]::WriteLine($file)
    [Console]::Out.Flush()
    [void][Console]::ReadLine()
}} finally {{
    [IO.Directory]::Delete($dir, $true)
}}
"""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return f"powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"
    decoder = "/usr/bin/base64 -D" if config.os == "macos" else "base64 -d"
    script = f"""
set -eu
umask 077
dir=$(mktemp -d "${{TMPDIR:-/tmp}}/meiva-chrome.XXXXXXXX")
trap 'rm -rf -- "$dir"' EXIT
trap 'exit 1' HUP INT TERM
file="$dir"/{shlex.quote(filename)}
IFS= read -r payload
printf '%s' "$payload" | {decoder} > "$file"
printf '%s\\n' "$file"
IFS= read -r close || true
"""
    return f"sh -c {shlex.quote(script)}"


@asynccontextmanager
async def stage_ssh_upload(
    config: RemoteHostConfig, filename: str, encoded: str
) -> AsyncGenerator[str, None]:
    filename = upload_name(filename)
    process = await asyncio.create_subprocess_exec(
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "--",
        config.ssh_host,
        _command(config, filename),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=4096,
    )
    sender, receiver = process.stdin, process.stdout
    if sender is None or receiver is None:
        process.kill()
        await process.wait()
        raise RuntimeError("SSH upload pipes unavailable")
    try:
        async with asyncio.timeout(_TIMEOUT_S):
            sender.write(encoded.encode("ascii") + b"\n")
            await sender.drain()
            line = (await receiver.readline()).decode("utf-8").rstrip("\r\n")
        path = PureWindowsPath(line) if config.os == "windows" else PurePosixPath(line)
        if not path.is_absolute() or path.name != filename:
            raise RuntimeError(f"SSH Chrome could not stage upload on {config.target_uri}")
        yield line
    finally:
        sender.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=_TIMEOUT_S)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
