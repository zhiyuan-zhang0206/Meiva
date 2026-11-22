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
_CLEANUP_S = 2.0


def _command(config: RemoteHostConfig, filename: str) -> str:
    if config.os == "windows":
        filename64 = base64.b64encode(filename.encode("utf-8")).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
$root = Join-Path ([IO.Path]::GetTempPath()) ('meiva-chrome-uploads-' + $sid.Value)
[void][IO.Directory]::CreateDirectory($root, $acl)
if ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) {{
    throw 'Invalid upload root'
}}
Set-Acl -LiteralPath $root -AclObject $acl
Get-ChildItem -LiteralPath $root -Directory -Filter 'upload-*' | Where-Object {{
    $_.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddMinutes(-15) -and
    !($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
}} | Remove-Item -Recurse -Force
$dir = Join-Path $root ('upload-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($dir, $acl)
$inputReader = [IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.Encoding]::ASCII)
try {{
    $name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{filename64}'))
    $file = Join-Path $dir $name
    $text = $inputReader.ReadLineAsync()
    if (!$text.Wait(12000)) {{ throw 'Upload input deadline expired' }}
    [IO.File]::WriteAllBytes($file, [Convert]::FromBase64String($text.Result))
    [Console]::WriteLine($file)
    [Console]::Out.Flush()
    [void]$inputReader.ReadLineAsync().Wait(600000)
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
root="${{TMPDIR:-/tmp}}/meiva-chrome-uploads-$(id -u)"
mkdir -p -- "$root"
test ! -L "$root" && test -O "$root"
chmod 700 -- "$root"
find "$root" -mindepth 1 -maxdepth 1 -type d -name 'upload.*' -mmin +15 -exec rm -rf -- {{}} +
dir=$(mktemp -d "$root/upload.XXXXXXXX")
trap 'rm -rf -- "$dir"' EXIT
trap 'exit 1' HUP INT TERM
file="$dir"/{shlex.quote(filename)}
IFS= read -r -t 12 payload
printf '%s' "$payload" | {decoder} > "$file"
printf '%s\\n' "$file"
IFS= read -r -t 600 close || true
"""
    return f"bash -c {shlex.quote(script)}"


@asynccontextmanager
async def stage_ssh_upload(
    config: RemoteHostConfig, filename: str, encoded: str
) -> AsyncGenerator[str, None]:
    filename = upload_name(filename)
    process = await asyncio.create_subprocess_exec(
        "ssh",
        "-T",
        "-S",
        "none",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
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
            await asyncio.wait_for(process.wait(), timeout=_CLEANUP_S)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
