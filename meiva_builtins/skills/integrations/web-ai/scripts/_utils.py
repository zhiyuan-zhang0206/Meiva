# meiva:note
# The effects around where a web-ai child run lands its files: the places it lands them in, handed in by the child's entry script (the composition root that reads both homes and calls `system_places`), and creating a capability's landing directory. The naming itself is `_landing`; the instant a run is named by is read in the entry and reaches it as a value.
from __future__ import annotations

from pathlib import Path

from _landing import Places, capability_dir


# meiva:note
# The places of this process: the Meiva `home` its entry script resolved (through the one resolver every Meiva process uses, without touching `Settings`, so no database is reached), and the account's `user_home`, under whose `Downloads` the run lands what it saves.
def system_places(home: Path, user_home: Path) -> Places:
    return Places(home=home, downloads=user_home / "Downloads")


# meiva:note
# `~/Downloads/meiva_<home-label>_web-ai/<capability>/`, created on demand.
def downloads_root(capability: str, *, places: Places) -> Path:
    root = capability_dir(places, capability)
    root.mkdir(parents=True, exist_ok=True)
    return root
