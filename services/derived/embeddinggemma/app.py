# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from services.derived.embeddinggemma.config import ServerConfig
from services.derived.embeddinggemma.contract import (
    DIM,
    DRAIN_S,
    EMBEDDINGS_PATH,
    HEALTH_PATH,
    SHUTDOWN_CEILING_S,
)
from services.derived.embeddinggemma.engine import (
    EmbeddingRejectedError,
    EngineOpener,
    OpenedEngine,
)
from services.derived.embeddinggemma.scheduler import InferenceScheduler, SchedulerClosedError

_log = logging.getLogger("embeddinggemma")


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[str]
    encoding_format: str = "float"
    dimensions: int = DIM
    normalize: bool = True
    insert_special_tokens: bool = True


class _Serving:
    def __init__(self) -> None:
        self.opened: OpenedEngine | None = None
        self.scheduler: InferenceScheduler | None = None


def _report_late_close(close: asyncio.Future[None]) -> None:
    if error := close.exception():
        _log.error("engine close failed after the shutdown deadline: %s", error)


@asynccontextmanager
async def _serve(
    _app: FastAPI, *, serving: _Serving, config: ServerConfig, open_engine: EngineOpener
) -> AsyncGenerator[None]:
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embeddinggemma")
    try:
        opened = await loop.run_in_executor(executor, open_engine, config)
        scheduler = InferenceScheduler(executor)
        serving.opened = opened
        serving.scheduler = scheduler
        _log.info(
            "engine ready: device=%s model=%s revision=%s max_input_length=%d signatures=%s "
            "open_ms=%.1f",
            config.device,
            config.model,
            config.revision,
            config.max_input_length,
            list(opened.signatures),
            opened.open_ms,
        )
        try:
            yield
        finally:
            serving.opened = None
            serving.scheduler = None
            scheduler.stop()
            close = loop.run_in_executor(executor, opened.engine.close)
            done, _ = await asyncio.wait({close}, timeout=SHUTDOWN_CEILING_S - DRAIN_S)
            if close in done:
                await close
            else:
                close.add_done_callback(_report_late_close)
                _log.warning("engine close exceeded the shutdown budget")
    finally:
        executor.shutdown(wait=False)


def create_app(
    config: ServerConfig, *, open_engine: EngineOpener, api_key: str | None = None
) -> FastAPI:
    if config.require_api_key and not api_key:
        raise RuntimeError(
            "the unit requires an API key but neither its environment nor $MEIVA_HOME/.env carries one; "
            "set MEIVA_EMBEDDINGGEMMA_API_KEY in the hosting machine's $MEIVA_HOME/.env "
            "(or in the service process environment)"
        )
    serving = _Serving()

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        if api_key is None:
            return
        presented = (authorization or "").removeprefix("Bearer ")
        if not hmac.compare_digest(presented.encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    app = FastAPI(lifespan=partial(_serve, serving=serving, config=config, open_engine=open_engine))

    @app.get(HEALTH_PATH)
    async def healthz() -> dict[str, object]:
        if serving.opened is None:
            raise HTTPException(status_code=503, detail="engine not initialized")
        return {
            "status": "ok",
            "model": config.model,
            "revision": config.revision,
            "device": config.device,
            "max_input_length": config.max_input_length,
            "signature_selection": list(serving.opened.signatures),
            "input_overflow_strategy": "ERROR",
            "open_ms": round(serving.opened.open_ms, 1),
        }

    @app.post(EMBEDDINGS_PATH, dependencies=[Depends(authorized)])
    async def create_embeddings(req: EmbeddingRequest) -> dict[str, object]:
        opened = serving.opened
        scheduler = serving.scheduler
        if opened is None or scheduler is None:
            raise HTTPException(status_code=503, detail="engine not initialized")
        if req.model != config.model:
            raise HTTPException(
                status_code=400,
                detail=f"model mismatch: requested {req.model!r}, server serves {config.model!r}",
            )
        if req.encoding_format != "float":
            raise HTTPException(
                status_code=400,
                detail=f"unsupported encoding_format {req.encoding_format!r}; only float is supported",
            )
        if req.dimensions != DIM:
            raise HTTPException(
                status_code=400, detail=f"unsupported dimensions {req.dimensions}; pinned to {DIM}"
            )
        texts = [req.input] if isinstance(req.input, str) else list(req.input)
        if not texts or any(not text.strip() for text in texts):
            raise HTTPException(status_code=400, detail="input must be non-empty string(s)")
        infer = partial(
            opened.engine.embed,
            texts,
            normalize=req.normalize,
            insert_special_tokens=req.insert_special_tokens,
            dimensions=req.dimensions,
        )
        try:
            vectors = await scheduler.run(infer, single=len(texts) == 1)
        except SchedulerClosedError as exc:
            raise HTTPException(status_code=503, detail="engine not initialized") from exc
        except EmbeddingRejectedError as exc:
            _log.warning("embedding rejected (batch=%d): %s", len(texts), exc)
            raise HTTPException(
                status_code=400,
                detail=f"embedding computation rejected input (overflow or invalid): {exc}",
            ) from exc
        return {
            "model": req.model,
            "data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)],
        }

    return app
