# meiva:note
# What the identity of the home's startup configuration is made of, as values: which files take part,
# how each is keyed and hashed and how the files combine into one digest. `start_inputs.py` finds
# the per-agent config files and reads them.

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

_FIXED_NAMES = (".env", "plugins_config.json")


# meiva:note
# The files that carry file-authoritative startup values, in digest order: the fixed files of the
# home, then the per-agent config files found, sorted.
def candidate_paths(home: Path, config_files: Iterable[Path]) -> list[Path]:
    return [*(home / name for name in _FIXED_NAMES), *sorted(config_files)]


# meiva:note
# The key of a file in the digest: its path relative to the home.
def relative_key(home: Path, path: Path) -> str:
    return str(path.relative_to(home))


# meiva:note
# The digest of one file's bytes.
def bytes_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# meiva:note
# The digest of the whole configuration from the digest of each file by key; None is a file that
# does not exist, which differs from an empty one.
def combined_digest(files: Mapping[str, str | None]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
