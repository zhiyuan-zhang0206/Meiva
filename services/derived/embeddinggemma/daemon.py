# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import uvicorn

from services.derived.embeddinggemma.app import create_app
from services.derived.embeddinggemma.config import ServerConfig, api_key_from, parse_args
from services.derived.embeddinggemma.contract import DRAIN_S
from services.derived.embeddinggemma.engine import EngineOpener, open_litert_engine

_LOG_FORMAT: Final = "%(asctime)s %(levelname)s [embeddinggemma] %(message)s"


def load_api_key(environ: Mapping[str, str]) -> str | None:
    home = environ.get("MEIVA_HOME", "").strip()
    text: str | None = None
    if home:
        try:
            text = (Path(home) / ".env").read_text()
        except OSError:
            text = None
    return api_key_from(environ, text)


def serve(config: ServerConfig, *, open_engine: EngineOpener, api_key: str | None) -> None:
    app = create_app(config, open_engine=open_engine, api_key=api_key)
    uvicorn.run(
        app,
        host=config.bind_host,
        port=config.port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=DRAIN_S,
    )


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    serve(
        parse_args(sys.argv[1:] if argv is None else argv),
        open_engine=open_litert_engine,
        api_key=load_api_key(os.environ),
    )


if __name__ == "__main__":
    main()
