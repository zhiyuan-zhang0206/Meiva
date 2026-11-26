# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from typing import Final

import uvicorn

from services.derived.embeddinggemma.app import create_app
from services.derived.embeddinggemma.config import API_KEY_ENV, ServerConfig, parse_args
from services.derived.embeddinggemma.engine import EngineOpener, open_litert_engine

_LOG_FORMAT: Final = "%(asctime)s %(levelname)s [embeddinggemma] %(message)s"


def serve(
    config: ServerConfig, *, open_engine: EngineOpener, environ: dict[str, str] | None = None
) -> None:
    env = os.environ if environ is None else environ
    app = create_app(config, open_engine=open_engine, api_key=env.get(API_KEY_ENV) or None)
    uvicorn.run(app, host=config.bind_host, port=config.port, log_level="warning", access_log=False)


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    serve(parse_args(sys.argv[1:] if argv is None else argv), open_engine=open_litert_engine)


if __name__ == "__main__":
    main()
