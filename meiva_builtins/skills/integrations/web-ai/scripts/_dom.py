# meiva:note
# The driver's click-by-visible-text and click-to-download controls: the accessibility tree is read through the chrome MCP, matched by `_pages.find_clickable`, and the match is clicked with a real input event. Text matching survives a redesign that only renames classes or data attributes, which CSS selectors do not.
from __future__ import annotations

import shutil
import time
from collections.abc import Sequence
from pathlib import Path

from _landing import DownloadEntry, download_settled, newest_new_download
from _pacing import STANDARD_PACE, Pacing
from _pages import Clickable, find_clickable

import meiva


# meiva:note
# The first rendered control whose accessible name matches one of `texts` (see `_pages.find_clickable` for the matching rules), or None.
def resolve_clickable(texts: Sequence[str]) -> Clickable | None:
    return find_clickable(meiva.mcps.chrome.take_snapshot(), texts)


# meiva:note
# Click the first rendered control that matches one of `texts` and return its label, or None when nothing matched (the caller decides whether that is fatal). The click dispatches a real input event, NOT element.click(), which several of these apps' menus ignore (no synthetic-event handler on the trigger).
def click_by_text(texts: Sequence[str]) -> str | None:
    hit = resolve_clickable(texts)
    if hit is None:
        return None
    meiva.mcps.chrome.click(uid=hit.uid)
    return hit.label


# meiva:note
# The finished-looking files of `downloads` with one of `suffixes`; a file that vanishes between the listing and its stat is skipped.
def _entries(downloads: Path, suffixes: Sequence[str]) -> list[DownloadEntry]:
    found: list[DownloadEntry] = []
    for path in downloads.iterdir():
        if path.suffix not in suffixes:
            continue
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        found.append(DownloadEntry(path.name, info.st_mtime, info.st_size))
    return found


# meiva:note
# The size of `path`, or None when the file is gone.
def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


# meiva:note
# Click a control that triggers a browser download and collect the landed file from `downloads` into `dest` (the full target path). The browser carries the session auth that a bare fetch of the asset URL does not. Returns None when the control was not found or no file landed in time; an in-flight download sits at .crdownload, so a suffix filter only sees completed files.
def download_by_click(
    texts: Sequence[str],
    *,
    dest: Path,
    suffixes: Sequence[str],
    downloads: Path,
    timeout: float = 60.0,
    pace: Pacing = STANDARD_PACE,
) -> Path | None:
    before = {entry.name for entry in _entries(downloads, suffixes)}
    if not click_by_text(texts):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(pace.download_poll)
        newest = newest_new_download(before, _entries(downloads, suffixes))
        if newest is None:
            continue
        time.sleep(pace.download_recheck)
        if download_settled(newest.size, _size(downloads / newest.name)):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(downloads / newest.name), dest)
            return dest
    return None
