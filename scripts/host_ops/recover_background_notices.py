#!/usr/bin/env python3
# meiva:note
# Replay this host's completion intents from an existing healthy-gateway hook.

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from base.host.boundary.options import options
from meiva.shell.completion_recovery import recover


@dataclass(frozen=True)
class Options:
    workspaces: Path
    cli: Path
    limit: int
    retry_after: float
    timeout: float


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Replay host-local completion intents after the gateway is healthy."
    )
    parser.add_argument("--workspaces", required=True, type=Path)
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--retry-after", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = options(parser.parse_args(argv), Options)
    result = recover(
        args.workspaces.resolve(strict=True),
        args.cli.resolve(strict=True),
        now=time.time(),
        limit=args.limit,
        retry_after=args.retry_after,
        timeout=args.timeout,
    )
    print(json.dumps(asdict(result)))
    return 3 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
