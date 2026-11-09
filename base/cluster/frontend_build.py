# meiva:link base/cluster/docs/frontend-build.md
# meiva:note
# The frontend's production build is reused only when every input that produced it
# is unchanged; the key lives inside `.next`, so deleting the build deletes the
# claim.

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

from base.host.boundary.options import attribute, options
from base.host.net.browser_origins import parse_browser_origins, split_browser_origins
from base.host.proc import run_bounded

_KEY_FILE = "meiva-build-key"
_FRONTEND_BIND_HOST = "127.0.0.1"
# meiva:note
# Loopback SYN/ACK or RST resolves inside the kernel in sub-millisecond time; 200ms
# bounds the preflight under scheduler contention without delaying normal startup.
_LOOPBACK_PROBE_TIMEOUT_S = 0.2
BuildAction = Literal["reuse", "build"]


@dataclass(frozen=True)
class BuildDecision:
    action: BuildAction
    message: str


def plan_frontend_build(
    frontend_dir: Path,
    *,
    current_key: str | None,
    recorded: str | None,
) -> BuildDecision:
    if current_key is not None and recorded == current_key:
        return BuildDecision(
            action="reuse",
            message=f"frontend build is current for {frontend_dir}; reusing .next",
        )
    return BuildDecision(
        action="build",
        message=f"frontend build inputs changed for {frontend_dir}; running pnpm run build",
    )


def _frontend_source_names(frontend_dir: Path) -> list[bytes]:
    result = run_bounded(
        [
            "git",
            "-C",
            str(frontend_dir),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        timeout=30.0,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed for {frontend_dir}: {stderr}")
    return sorted(set((result.stdout or b"").split(b"\0")) - {b""})


def _source_entry_record(path: Path, encoded: bytes) -> bytes:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return b"D\0" + encoded + b"\0"
    if stat.S_ISLNK(mode):
        content = str(path.readlink()).encode("utf-8")
    elif stat.S_ISREG(mode):
        content = path.read_bytes()
    else:
        raise RuntimeError(f"unsupported frontend source member: {path}")
    return (
        b"F\0" + encoded + b"\0" + f"{mode:o}\0".encode("ascii") + hashlib.sha256(content).digest()
    )


# meiva:note
# Hash every tracked or untracked-non-ignored path, mode and content under
# `frontend_dir` without binding the repository's `HEAD`, so commits outside
# `frontend_dir` keep the same digest while mid-read edits or checkouts fail closed.
# meiva:link base/cluster/docs/frontend-build.md
def frontend_source_digest(frontend_dir: Path) -> str:
    if not frontend_dir.is_dir():
        raise FileNotFoundError(frontend_dir)
    names = _frontend_source_names(frontend_dir)
    digest = hashlib.sha256(b"meiva-frontend-source-v1\0")
    for encoded in names:
        digest.update(_source_entry_record(frontend_dir / encoded.decode("utf-8"), encoded))
    if _frontend_source_names(frontend_dir) != names:
        raise RuntimeError(f"frontend source file list changed while reading: {frontend_dir}")
    return digest.hexdigest()


def build_key(
    frontend_dir: Path, *, gateway_port: int, browser_origins: tuple[str, ...]
) -> str | None:
    # meiva:note
    # None means "cannot prove the build is current", and the caller treats that as
    # "build". The source digest covers every tracked or untracked-but-not-ignored
    # file under `frontend_dir` without binding the monorepo's `HEAD`, so backend-only
    # commits reuse `.next` while any frontend edit or dependency change rebuilds.
    try:
        source = frontend_source_digest(frontend_dir)
    except (OSError, RuntimeError, UnicodeDecodeError, subprocess.SubprocessError):
        return None
    node = _node_version()
    if node is None:
        return None
    return key_from_parts(source, node, gateway_port=gateway_port, browser_origins=browser_origins)


def key_from_parts(
    source: str, node: str, *, gateway_port: int, browser_origins: tuple[str, ...]
) -> str:
    body = json.dumps(
        {
            "source": source,
            "node": node,
            "gateway_port": gateway_port,
            "browser_origins": browser_origins,
        },
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _node_version() -> str | None:
    try:
        result = run_bounded(["node", "--version"], timeout=30.0, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def key_path(frontend_dir: Path) -> Path:
    return frontend_dir / ".next" / _KEY_FILE


def recorded_key(frontend_dir: Path) -> str | None:
    try:
        return key_path(frontend_dir).read_text().strip() or None
    except OSError:
        return None


def is_current(frontend_dir: Path, key: str) -> bool:
    return recorded_key(frontend_dir) == key


def record(frontend_dir: Path, key: str) -> None:
    path = key_path(frontend_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n")


def _pnpm_build(frontend_dir: Path) -> int:
    return subprocess.run(["pnpm", "run", "build"], cwd=str(frontend_dir), check=False).returncode


def finalize_frontend_build(
    frontend_dir: Path,
    *,
    current_key: str | None,
    exit_code: int,
) -> int:
    # meiva:note
    # The key is recorded only after the build succeeded, so an interrupted rebuild
    # can never look current: its inputs differ from the recorded key by
    # construction, and the next start rebuilds.
    if exit_code != 0:
        return exit_code
    if current_key is not None:
        record(frontend_dir, current_key)
    return 0


# meiva:note
# Check `127.0.0.1:<port>` before `pnpm run build` mutates `.next` in place: if an
# orphaned or foreign listener still holds the serve port, rebuilding first would
# overwrite the live `.next` chunks on disk and then immediately crash on EADDRINUSE.
# Combining a loopback TCP connect probe with an `SO_REUSEADDR` bind check catches
# wildcard (`0.0.0.0`) and loopback listeners alike while allowing `TIME_WAIT` sockets
# left behind by a just-stopped generation, matching Node/libuv's own `uv_tcp_bind`.
def port_busy_reason(port: int, *, host: str = _FRONTEND_BIND_HOST) -> str | None:
    if not (1 <= port <= 65535):
        return f"{host}:{port} is not a valid TCP port"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(_LOOPBACK_PROBE_TIMEOUT_S)
        if probe.connect_ex((host, port)) == 0:
            return f"{host}:{port} already has an active TCP listener"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as binder:
        binder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            binder.bind((host, port))
        except OSError as exc:
            return f"{host}:{port} cannot be bound ({exc.strerror or exc})"
    return None


# meiva:note
# The decision is printed because a unit's stdout is the only record an operator
# can read after a start: this one-shot module has no CLI process to carry a log
# sink, and "the UI did not change" must not look like "the build was skipped by
# accident".
def ensure_build(
    frontend_dir: Path,
    *,
    gateway_port: int,
    browser_origins: tuple[str, ...],
    port: int | None = None,
    build_runner: Callable[[Path], int] = _pnpm_build,
) -> int:
    if port is not None:
        busy = port_busy_reason(port)
        if busy is not None:
            sys.stderr.write(
                f"frontend port preflight failed: {busy}; refusing to overwrite {frontend_dir / '.next'}\n"
            )
            return 1
    current = build_key(frontend_dir, gateway_port=gateway_port, browser_origins=browser_origins)
    decision = plan_frontend_build(
        frontend_dir,
        current_key=current,
        recorded=recorded_key(frontend_dir),
    )
    print(decision.message)  # noqa: T201
    match decision.action:
        case "reuse":
            return 0
        case "build":
            code = build_runner(frontend_dir)
            return finalize_frontend_build(frontend_dir, current_key=current, exit_code=code)
        case _:
            assert_never(decision.action)


# meiva:note
# The options of the `ensure` command, the only one, read from the parsed namespace by `options`.
@dataclass(frozen=True)
class _EnsureOptions:
    dir: str
    gateway_port: int
    port: int | None
    browser_origins: str


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="base.cluster.frontend_build")
    sub = parser.add_subparsers(dest="command", required=True)
    ensure = sub.add_parser("ensure", help="build the frontend unless its inputs are unchanged")
    ensure.add_argument("--dir", required=True, help="frontend directory, relative to the unit cwd")
    ensure.add_argument("--gateway-port", type=int, required=True)
    ensure.add_argument("--port", type=int, default=None)
    ensure.add_argument("--browser-origins", default="")
    args = parser.parse_args(argv)
    if attribute(args, "command", str) == "ensure":
        chosen = options(args, _EnsureOptions)
        return ensure_build(
            Path(chosen.dir),
            gateway_port=chosen.gateway_port,
            browser_origins=parse_browser_origins(split_browser_origins(chosen.browser_origins)),
            port=chosen.port,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
