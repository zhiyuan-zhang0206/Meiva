from __future__ import annotations

import hashlib
import http.client
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager

DOWNLOAD_ERRORS = (OSError, http.client.HTTPException)

RUNTIME_PINS_FILENAME = ".artifact-pins"


def runtime_pin_manifest(
    platform: str, pg_version: str, pg_sha: str, vector_version: str, vector_sha: str
) -> str:
    return f"{platform}\n{pg_version} {pg_sha}\n{vector_version} {vector_sha}\n"


def is_transient_artifact_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    return isinstance(exc, DOWNLOAD_ERRORS)


def fetch_verified(
    urls: tuple[str, ...],
    expected_sha256: str,
    *,
    open_url: Callable[
        [urllib.request.Request, float], AbstractContextManager[http.client.HTTPResponse]
    ],
    sleep: Callable[[float], None],
    report_failure: Callable[[str], None],
) -> bytes:
    last_err: Exception | None = None
    remaining = list(urls)
    for attempt in range(1, 6):
        for url in remaining.copy():
            try:
                req = urllib.request.Request(  # noqa: S310 — artifact pins supply HTTPS URLs
                    url, headers={"User-Agent": "meiva-ci-image-builder/1.0"}
                )
                with open_url(req, 120.0) as resp:
                    blob = resp.read()
            except DOWNLOAD_ERRORS as exc:
                last_err = exc
                report_failure(f"download source {url} failed: {exc}")
                if not is_transient_artifact_error(exc):
                    remaining.remove(url)
                continue
            actual = hashlib.sha256(blob).hexdigest()
            if actual != expected_sha256:
                raise RuntimeError(
                    f"sha256 mismatch for {url}: expected {expected_sha256}, got {actual}"
                )
            return blob
        if not remaining:
            break
        if attempt < 5:
            sleep(attempt * 3)
    raise RuntimeError(f"failed to fetch {', '.join(urls)}: {last_err}") from last_err
