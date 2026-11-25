from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 5
GZIP_EXCLUDED_CONTENT_TYPES = (
    *DEFAULT_EXCLUDED_CONTENT_TYPES,
    "application/octet-stream",
    "application/pdf",
    "application/x-tar",
    "application/zstd",
)


def install_response_compression(app: FastAPI) -> None:
    app.add_middleware(
        GZipMiddleware,
        minimum_size=GZIP_MIN_BYTES,
        compresslevel=GZIP_LEVEL,
        exclude_content_types=GZIP_EXCLUDED_CONTENT_TYPES,
    )
