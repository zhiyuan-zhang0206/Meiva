# meiva:link base/cluster/docs/frontend-build.meiva.okf.md
# meiva:note
# The frontend's production build is reused only when every input that produced it
# is unchanged; the key lives inside `.next`, so deleting the build deletes the
# claim.

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from base.host.runtime_interpreter import source_digest

_KEY_FILE = "meiva-build-key"


def build_key(frontend_dir: Path, *, gateway_port: int, browser_origin: str) -> str | None:
    # meiva:note
    # None means "cannot prove the build is current", and the caller treats that as
    # "build". The source digest covers HEAD and every tracked or
    # untracked-but-not-ignored file under the frontend, so a checkout move or an
    # edited component both change the key.
    try:
        source = source_digest(frontend_dir)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    node = _node_version()
    if node is None:
        return None
    return key_from_parts(source, node, gateway_port=gateway_port, browser_origin=browser_origin)


def key_from_parts(source: str, node: str, *, gateway_port: int, browser_origin: str) -> str:
    body = json.dumps(
        {
            "source": source,
            "node": node,
            "gateway_port": gateway_port,
            "browser_origin": browser_origin,
        },
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _node_version() -> str | None:
    try:
        result = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


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


def _npm_build(frontend_dir: Path) -> int:
    return subprocess.run(["npm", "run", "build"], cwd=str(frontend_dir), check=False).returncode


# meiva:note
# The decision is printed because a unit's stdout is the only record an operator
# can read after a start: this one-shot module has no CLI process to carry a log
# sink, and "the UI did not change" must not look like "the build was skipped by
# accident".
def ensure_build(
    frontend_dir: Path,
    *,
    gateway_port: int,
    browser_origin: str,
    build: Callable[[Path], int] = _npm_build,
    key: Callable[..., str | None] = build_key,
) -> int:
    # meiva:note
    # The key is recorded only after the build succeeded, so an interrupted rebuild
    # can never look current: its inputs differ from the recorded key by
    # construction, and the next start rebuilds.
    current = key(frontend_dir, gateway_port=gateway_port, browser_origin=browser_origin)
    if current is not None and is_current(frontend_dir, current):
        print(f"frontend build is current for {frontend_dir}; reusing .next")  # noqa: T201
        return 0
    print(f"frontend build inputs changed for {frontend_dir}; running npm run build")  # noqa: T201
    code = build(frontend_dir)
    if code != 0:
        return code
    if current is not None:
        record(frontend_dir, current)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="base.cluster.frontend_build")
    sub = parser.add_subparsers(dest="command", required=True)
    ensure = sub.add_parser("ensure", help="build the frontend unless its inputs are unchanged")
    ensure.add_argument("--dir", required=True, help="frontend directory, relative to the unit cwd")
    ensure.add_argument("--gateway-port", type=int, required=True)
    ensure.add_argument("--browser-origin", default="")
    args = parser.parse_args(argv)
    if args.command == "ensure":
        return ensure_build(
            Path(args.dir), gateway_port=args.gateway_port, browser_origin=args.browser_origin
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
