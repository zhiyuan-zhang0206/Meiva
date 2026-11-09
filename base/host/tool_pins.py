from __future__ import annotations

import subprocess
from pathlib import Path

# meiva:link base/host/docs/tool-pins.md
UV_VERSION = "0.13.1"

# meiva:link base/host/docs/tool-pins.md
PNPM_VERSION = "11.9.0"

UV_ASSET_SHA256: dict[str, str] = {
    "aarch64-apple-darwin": "cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2",
    "x86_64-apple-darwin": "bb7b805bdcf3d0bc8c74ceb94569e6b3e8aa35bd492acdbb9d46bf46d9d4ff6b",
    "aarch64-unknown-linux-gnu": "9041c73c99fdc5892cf128140beb354eca9231d502068cc36e6cb53a8e59ad14",
    "x86_64-unknown-linux-gnu": "eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220",
}

# meiva:link base/host/docs/tool-pins.md
PGBOUNCER_APT_VERSION = "1.26.0-1.pgdg24.04+1"

REDIS_APT_VERSION = "6:8.2.*"


def brew_prefix(formula: str = "") -> Path:
    cmd = ["brew", "--prefix", *([formula] if formula else [])]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return Path("/opt/homebrew")
    return Path(out.stdout.strip() or "/opt/homebrew")
