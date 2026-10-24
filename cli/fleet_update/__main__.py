from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import main
from .session import Pacing

if __name__ == "__main__":
    raise SystemExit(
        main(
            sys.argv[1:],
            repo=Path(__file__).resolve().parents[2],
            env=os.environ,
            now=datetime.now(UTC),
            pacing=Pacing(),
        )
    )
