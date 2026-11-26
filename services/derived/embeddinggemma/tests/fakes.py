from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from services.derived.embeddinggemma.config import ServerConfig
from services.derived.embeddinggemma.contract import DIM
from services.derived.embeddinggemma.engine import (
    EmbeddingRejectedError,
    LiteRtRuntime,
    OpenedEngine,
    RawEngine,
)

MODEL_BYTES = b"pinned-artifact-bytes"
REVISION = hashlib.sha256(MODEL_BYTES).hexdigest()
MODEL = "embeddinggemma-2"


def server_config(tmp_path: Path, **changes: object) -> ServerConfig:
    model_path = tmp_path / "model.litertlm"
    model_path.write_bytes(MODEL_BYTES)
    base = ServerConfig(
        model_path=str(model_path),
        device="cpu",
        revision=REVISION,
        model=MODEL,
        cache_dir=str(tmp_path / "cache"),
        max_input_length=8192,
        bind_host="127.0.0.1",
        port=9379,
    )
    return replace(base, **changes)


def vector(seed: int) -> list[float]:
    return [float(seed) if column == 0 else 0.0 for column in range(DIM)]


@dataclass
class FakeEngine:
    overflow_over: int | None = None
    calls: list[tuple[list[str], bool, bool, int]] = field(default_factory=list)
    closed: int = 0

    def embed(
        self,
        texts: Sequence[str],
        *,
        normalize: bool,
        insert_special_tokens: bool,
        dimensions: int,
    ) -> list[list[float]]:
        self.calls.append((list(texts), normalize, insert_special_tokens, dimensions))
        if self.overflow_over is not None and any(len(t) > self.overflow_over for t in texts):
            raise EmbeddingRejectedError("input exceeds max_input_length")
        return [vector(len(text)) for text in texts]

    def close(self) -> None:
        self.closed += 1


def opener(engine: FakeEngine, *, signatures: tuple[int, ...] = (1,)):
    def open_engine(_config: ServerConfig) -> OpenedEngine:
        return OpenedEngine(engine, signatures, 12.5)

    return open_engine


@dataclass
class _Response:
    embedding: list[float]


@dataclass
class FakeRaw:
    fail_with: Exception | None = None
    closed: int = 0
    options_seen: list[object] = field(default_factory=list)

    def compute_embedding_batch(
        self, texts: Sequence[str], *, options: object
    ) -> Sequence[_Response]:
        self.options_seen.append(options)
        if self.fail_with is not None:
            raise self.fail_with
        return [_Response(vector(len(text))) for text in texts]

    def close(self) -> None:
        self.closed += 1


@dataclass
class FakeRuntime:
    raw: FakeRaw = field(default_factory=FakeRaw)
    opened_with: list[tuple[str, object, str, int]] = field(default_factory=list)

    def runtime(self) -> LiteRtRuntime:
        def open_raw(model_path: str, backend: object, cache_dir: str, max_len: int) -> RawEngine:
            self.opened_with.append((model_path, backend, cache_dir, max_len))
            return self.raw

        return LiteRtRuntime(
            cpu_backend=lambda: "CPU",
            gpu_backend=lambda: "GPU",
            open_raw=open_raw,
            options=lambda normalize, special, dims: ("options", normalize, special, dims),
            signatures=lambda _path: (7,),
        )
