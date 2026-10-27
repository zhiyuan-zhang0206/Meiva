# meiva:note
# The effects around where a web-ai child run lands its files: reading the Meiva home and the Downloads folder once (the child's entry script is the composition root that calls `system_places`), creating a capability's landing directory, and reading the clock for the run stamp. The naming itself is `_landing`.
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from _landing import Places, capability_dir, format_stamp


# meiva:note
# The places of this process: the Meiva `home` its entry script resolved (through the one resolver every Meiva process uses, without touching `Settings`, so no database is reached), and `~/Downloads`.
def system_places(home: Path) -> Places:
    return Places(home=home, downloads=Path("~/Downloads").expanduser())


# meiva:note
# `~/Downloads/meiva_<home-label>_web-ai/<capability>/`, created on demand.
def downloads_root(capability: str, *, places: Places) -> Path:
    root = capability_dir(places, capability)
    root.mkdir(parents=True, exist_ok=True)
    return root


# meiva:note
# The local-time `YYYYmmdd-HHMMSS` stamp for naming a run's output directory, read from the clock now.
def now_stamp() -> str:
    return format_stamp(_dt.datetime.now().astimezone())
