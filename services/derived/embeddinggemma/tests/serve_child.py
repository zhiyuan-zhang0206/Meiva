from __future__ import annotations

import sys
from pathlib import Path

from services.derived.embeddinggemma import daemon
from services.derived.embeddinggemma.config import ServerConfig
from services.derived.embeddinggemma.engine import OpenedEngine
from services.derived.embeddinggemma.tests.fakes import FakeEngine, server_config


class _MarkingEngine(FakeEngine):
    def __init__(self, marker: Path) -> None:
        super().__init__()
        self._marker = marker

    def close(self) -> None:
        super().close()
        self._marker.write_text("closed")


def main() -> None:
    scratch, port = Path(sys.argv[1]), int(sys.argv[2])
    config = server_config(scratch, port=port)
    engine = _MarkingEngine(scratch / "closed.marker")

    def open_engine(_config: ServerConfig) -> OpenedEngine:
        return OpenedEngine(engine, (1,), 0.0)

    daemon.serve(config, open_engine=open_engine, api_key=None)


if __name__ == "__main__":
    main()
