# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

import hashlib
import importlib
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol, runtime_checkable

from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_of
from services.derived.embeddinggemma.config import ServerConfig
from services.derived.embeddinggemma.contract import DIM

WARMUP_TEXT = "task: search result | query: warmup"
_HASH_CHUNK_BYTES = 1024 * 1024


class EmbeddingRejectedError(Exception): ...


class Engine(Protocol):
    def embed(
        self,
        texts: Sequence[str],
        *,
        normalize: bool,
        insert_special_tokens: bool,
        dimensions: int,
    ) -> list[list[float]]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class OpenedEngine:
    engine: Engine
    signatures: tuple[int, ...]
    open_ms: float


EngineOpener = Callable[[ServerConfig], OpenedEngine]


def verify_sha256(path: str, expected: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"model SHA256 mismatch for {path}: file is {actual}, "
            f"MEIVA_EMBEDDINGGEMMA_REVISION pins {expected}"
        )
    return actual


class _Vector(Protocol):
    @property
    def embedding(self) -> Sequence[float]: ...


@runtime_checkable
class RawEngine(Protocol):
    def compute_embedding_batch(
        self, texts: Sequence[str], *, options: object
    ) -> Sequence[_Vector]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class LiteRtRuntime:
    cpu_backend: Callable[[], object]
    gpu_backend: Callable[[], object]
    open_raw: Callable[[str, object, str, int], RawEngine]
    options: Callable[[bool, bool, int], object]
    signatures: Callable[[str], tuple[int, ...]]


def load_runtime(
    litert_path: str | None,
    *,
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> LiteRtRuntime:
    if litert_path is not None and litert_path not in sys.path:
        sys.path.append(litert_path)
    try:
        interfaces = import_module("litert_lm.interfaces")
        embedding_engine = import_module("litert_lm.embedding_engine")
        model_info = import_module("litert_lm.model_info")
    except ImportError as exc:
        raise RuntimeError(
            "litert_lm is not importable: install litert-lm in the interpreter named by "
            "MEIVA_EMBEDDINGGEMMA_PYTHON, or point MEIVA_EMBEDDINGGEMMA_LITERT_PATH at a "
            f"directory holding it ({exc})"
        ) from exc
    overflow = object_of(
        getattr, object_of(getattr, embedding_engine, "InputOverflowStrategy"), "ERROR"
    )
    options_class = object_of(getattr, embedding_engine, "EmbeddingOptions")
    engine_class = object_of(getattr, embedding_engine, "EmbeddingEngine")
    model_info_class = object_of(getattr, model_info, "ModelInfo")
    cpu_class = object_of(getattr, interfaces, "CPU")
    gpu_class = object_of(getattr, interfaces, "GPU")

    def options(normalize: bool, special: bool, dimensions: int) -> object:  # noqa: FBT001
        return _call(
            options_class,
            normalize=normalize,
            insert_special_tokens=special,
            input_overflow_strategy=overflow,
            output_size=dimensions,
        )

    def open_raw(model_path: str, backend: object, cache_dir: str, max_len: int) -> RawEngine:
        raw = _call(
            engine_class,
            model_path=model_path,
            backend=backend,
            cache_dir=cache_dir,
            max_input_length=max_len,
        )
        return _raw_engine(raw)

    def signatures(model_path: str) -> tuple[int, ...]:
        info = _call(model_info_class, model_path)
        selection = object_of(getattr, object_of(getattr, info, "embedding"), "signature_selection")
        if not is_object_list(selection):
            raise TypeError(f"signature_selection is {type(selection).__name__}, not a list")
        return tuple(item for item in selection if isinstance(item, int))

    return LiteRtRuntime(
        cpu_backend=lambda: _call(cpu_class),
        gpu_backend=lambda: _call(gpu_class),
        open_raw=open_raw,
        options=options,
        signatures=signatures,
    )


def _call(function: object, *args: object, **kwargs: object) -> object:
    if not callable(function):
        raise TypeError(f"expected a callable, got {type(function).__name__}")
    return object_of(function, *args, **kwargs)


def _raw_engine(raw: object) -> RawEngine:
    if not isinstance(raw, RawEngine):
        raise TypeError("litert_lm EmbeddingEngine lacks compute_embedding_batch or close")
    return raw


class _LiteRtEngine:
    def __init__(self, raw: RawEngine, runtime: LiteRtRuntime) -> None:
        self._raw = raw
        self._runtime = runtime

    def embed(
        self,
        texts: Sequence[str],
        *,
        normalize: bool,
        insert_special_tokens: bool,
        dimensions: int,
    ) -> list[list[float]]:
        options = self._runtime.options(normalize, insert_special_tokens, dimensions)
        try:
            responses = self._raw.compute_embedding_batch(list(texts), options=options)
        except Exception as exc:
            raise EmbeddingRejectedError(str(exc)) from exc
        return [[float(value) for value in response.embedding] for response in responses]

    def close(self) -> None:
        self._raw.close()


def open_litert_engine(
    config: ServerConfig,
    *,
    runtime: LiteRtRuntime | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> OpenedEngine:
    started = clock()
    Path(config.cache_dir).mkdir(parents=True, exist_ok=True)
    _ = verify_sha256(config.model_path, config.revision)
    litert = load_runtime(config.litert_path) if runtime is None else runtime
    backend = litert.cpu_backend() if config.device == "cpu" else litert.gpu_backend()
    signatures = litert.signatures(config.model_path)
    raw = litert.open_raw(config.model_path, backend, config.cache_dir, config.max_input_length)
    engine = _LiteRtEngine(raw, litert)
    try:
        _ = engine.embed([WARMUP_TEXT], normalize=True, insert_special_tokens=True, dimensions=DIM)
    except BaseException:
        engine.close()
        raise
    return OpenedEngine(engine, signatures, (clock() - started) * 1000.0)
