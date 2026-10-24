# meiva:note
# Where the web-ai children land what they produce, and which file in the Downloads folder is the one a browser download just finished. Paths are built from values the entry script read once (the Meiva home and the Downloads folder); nothing here touches the filesystem, the clock or the environment.
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_GROUP = "web-ai"


# meiva:note
# The two places a child run depends on: the Meiva home (its basename labels the landing directory) and the system Downloads folder (where the browser saves a download and where artifacts land).
@dataclass(frozen=True)
class Places:
    home: Path
    downloads: Path


# meiva:note
# One file of the Downloads folder as the download collector sees it.
@dataclass(frozen=True)
class DownloadEntry:
    name: str
    mtime: float
    size: int


# meiva:note
# This unit's display label: its home's basename with leading dots stripped (`~/.meiva` is "meiva", `~/.meiva-t1` is "meiva-t1"), or "meiva" when nothing is left. Path-only identity, so no settings are touched.
def cluster_label(home: Path) -> str:
    return home.name.lstrip(".") or "meiva"


# meiva:note
# `<downloads>/meiva_<home-label>_web-ai/<capability>/`, the landing directory of a capability's artifacts.
def capability_dir(places: Places, capability: str) -> Path:
    return places.downloads / f"meiva_{cluster_label(places.home)}_{_GROUP}" / capability


# meiva:note
# The local-time `YYYYmmdd-HHMMSS` stamp that names a run's output directory.
def format_stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%d-%H%M%S")


# meiva:note
# The newest file that was not there before the click (`before` holds the names present then), or None. An in-flight download sits under a different suffix, so the caller lists only completed names.
def newest_new_download(
    before: Collection[str], entries: Collection[DownloadEntry]
) -> DownloadEntry | None:
    fresh = [entry for entry in entries if entry.name not in before]
    return max(fresh, key=lambda entry: entry.mtime, default=None)


# meiva:note
# Whether a candidate download finished: non-empty and the same size at two looks apart. `size_now` is None when the file is gone.
def download_settled(size_before: int, size_now: int | None) -> bool:
    return size_before > 0 and size_now == size_before
