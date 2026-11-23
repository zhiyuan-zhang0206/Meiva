from __future__ import annotations

import hashlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from base.cluster.dataplane.artifact_fetch import fetch_verified
from base.cluster.dataplane.runtime_binaries import download_with_opener
from base.host.net.ports import SYSTEM_URL_OPENER

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _REPO_ROOT / ".github/workflows"
_SYNC_MARKERS = ("actions/uv-sync", "uv sync --frozen")
_VENDOR_ACTION = "./.github/actions/vendor-pg-runtime"


def test_every_job_that_installs_the_data_plane_also_vendors_the_postgres_runtime() -> None:
    install_jobs = [
        (workflow.name, job)
        for workflow in sorted(_WORKFLOWS.glob("*.yml"))
        for job in _job_bodies(workflow)
        if "./.github/actions/install-pg-redis" in job
    ]
    assert install_jobs, "no workflow installs the data plane"
    assert [name for name, job in install_jobs if _VENDOR_ACTION not in job] == []


def _job_bodies(workflow: Path) -> list[str]:
    return re.split(r"\n  (?=[a-z0-9-]+:\n)", workflow.read_text(encoding="utf-8"))


def test_vendored_runtime_action_is_keyed_on_the_pin_file_and_runs_after_deps() -> None:
    text = (_REPO_ROOT / ".github/actions/vendor-pg-runtime/action.yml").read_text(encoding="utf-8")
    assert "hashFiles('base/cluster/dataplane/runtime_binaries.py')" in text
    assert 'uv run python scripts/ci/vendor_pg_runtime.py "$GITHUB_ENV"' in text
    for workflow in _WORKFLOWS.glob("*.yml"):
        for job in _job_bodies(workflow):
            if _VENDOR_ACTION not in job:
                continue
            sync = min((job.find(marker) for marker in _SYNC_MARKERS if marker in job), default=-1)
            assert 0 <= sync < job.index("vendor-pg-runtime"), (
                f"{workflow.name}: vendored runtime built before the Python deps"
            )


def test_pgvector_smokes_use_the_cached_runtime() -> None:
    command = "uv run python scripts/ci/pgvector_runtime_smoke.py"
    for name in ("ci.yml", "release-gate.yml"):
        (smoke,) = [job for job in _job_bodies(_WORKFLOWS / name) if command in job]
        assert smoke.index(_VENDOR_ACTION) < smoke.index(command)
        assert 'MEIVA_HOME="$(dirname "$CI_VENDORED_RUNTIME_ROOT")"' in smoke


def test_the_session_home_links_the_vendored_runtime_the_job_exports() -> None:
    script = (_REPO_ROOT / "scripts/ci/vendor_pg_runtime.py").read_text(encoding="utf-8")
    bootstrap = (_REPO_ROOT / "tests/fixtures/env_bootstrap.py").read_text(encoding="utf-8")
    assert 'f"CI_VENDORED_RUNTIME_ROOT={root}' in script
    assert 'os.environ.get("CI_VENDORED_RUNTIME_ROOT")' in bootstrap
    assert 'TEST_MEIVA_HOME / "runtime"' in bootstrap


def test_ci_no_longer_installs_an_apt_pgvector() -> None:
    text = (_REPO_ROOT / ".github/actions/install-pg-redis/action.yml").read_text(encoding="utf-8")
    assert "postgresql-17-pgvector" not in text


@pytest.mark.parametrize("implementation", ["runtime", "image"])
@pytest.mark.parametrize(
    ("statuses", "expected_paths"),
    [
        ((200, 200), ["/primary", "/fallback"]),
        ((404, 410), ["/primary", "/fallback"]),
        ((410, 429, 200), ["/primary", "/fallback", "/fallback"]),
    ],
)
def test_artifact_download_only_retries_transient_sources(
    implementation: str, statuses: tuple[int, ...], expected_paths: list[str]
) -> None:
    responses = iter(statuses)
    paths: list[str] = []
    sleeps: list[float] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            status = next(responses, 500)
            body = b"x" if self.path == "/primary" else b"verified artifact"
            self.send_response(status)
            self.send_header("Content-Length", str(101 if self.path == "/primary" else len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    primary, fallback = (f"http://127.0.0.1:{server.server_port}{p}" for p in expected_paths[:2])
    try:

        def fetch() -> bytes:
            if implementation == "image":
                return fetch_verified(
                    (primary, fallback),
                    hashlib.sha256(b"verified artifact").hexdigest(),
                    open_url=SYSTEM_URL_OPENER,
                    sleep=sleeps.append,
                    report_failure=lambda _: None,
                )
            return download_with_opener(
                primary, open_url=SYSTEM_URL_OPENER, fallback_url=fallback, sleep=sleeps.append
            )

        if statuses[-1] == 410:
            with pytest.raises(RuntimeError, match=r"HTTP Error (404|410)"):
                _ = fetch()
        else:
            assert fetch() == b"verified artifact"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert paths == expected_paths
    retry_delay = 2 if implementation == "runtime" else 3
    assert sleeps == ([retry_delay] if len(statuses) == 3 else [])


def test_ci_image_copies_and_uses_the_artifact_downloader() -> None:
    dockerfile = (_REPO_ROOT / ".github/ci-image/Dockerfile").read_text(encoding="utf-8")
    dockerignore = (_REPO_ROOT / ".github/ci-image/Dockerfile.dockerignore").read_text(
        encoding="utf-8"
    )
    source = "base/cluster/dataplane/artifact_fetch.py"
    assert f"{source} /tmp/" in dockerfile
    assert f"!{source}" in dockerignore
    assert "from artifact_fetch import fetch_verified" in dockerfile
