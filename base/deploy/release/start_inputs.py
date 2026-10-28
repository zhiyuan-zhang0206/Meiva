"""Settings-free identity of the home's authoritative startup configuration."""

from __future__ import annotations

from pathlib import Path

from base.deploy.release.start_digest import (
    bytes_digest,
    candidate_paths,
    combined_digest,
    relative_key,
)
from base.host.verified_file.reader import regular_bytes


def configuration_digest(home: Path) -> str:
    """Include file-authoritative values omitted from child environment transport."""
    paths = candidate_paths(home, (home / "configs").glob("*/config.json"))
    return combined_digest({relative_key(home, path): _file_digest(path) for path in paths})


def _file_digest(path: Path) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    # A dangling symlink is an invalid declaration, never an omitted/default
    # value. Disappearance or replacement after lstat also refuses this read.
    return bytes_digest(regular_bytes(path))
