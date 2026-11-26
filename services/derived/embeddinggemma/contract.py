# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

import re
from typing import Final, Literal, get_args

DIM: Final = 768

DEFAULT_MODEL: Final = "embeddinggemma-2"

DEFAULT_PORT: Final = 9379

Device = Literal["cpu", "gpu"]

LOOPBACK: Final = "127.0.0.1"
EMBEDDINGS_PATH: Final = "/v1/embeddings"
HEALTH_PATH: Final = "/healthz"

_SHA256 = re.compile(r"[0-9a-f]{64}")


def parse_device(value: str) -> Device:
    normalized = value.strip().lower()
    if normalized == "cpu":
        return "cpu"
    if normalized == "gpu":
        return "gpu"
    raise ValueError(
        f"unsupported EmbeddingGemma device {value!r}; expected one of {list(get_args(Device))}"
    )


def require_artifact_sha256(revision: str | None) -> str:
    if revision is None or _SHA256.fullmatch(revision) is None:
        raise ValueError(
            "the managed EmbeddingGemma service needs MEIVA_EMBEDDINGGEMMA_REVISION to be the "
            "lowercase 64-hex SHA256 of the served model file"
        )
    return revision
