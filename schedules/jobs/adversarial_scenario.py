# meiva:note
# The scenario directories of the weekly batch and the loopback server some of them point at.
#
# A case builds a disposable scenario directory (files `adversarial_cases.scenario_files`
# decides), the batch reads back what the probe left in it, and `evaluate_case` scores it. The
# batch owns one counted loopback HTTP server in a lexical scope: one executor thread serves
# requests while the batch updates the partner document and collects counts, and leaving the
# scope, including by an error, shuts the server down, joins the thread and closes the socket.
# A failure of the serving loop surfaces when the scope is left.
# meiva:link schedules/docs/adversarial-eval.meiva.okf.md

from __future__ import annotations

import threading
from collections.abc import Generator, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from schedules.plans.adversarial_cases import (
    CASE_DEFINITIONS,
    AuditResult,
    CaseObservations,
    evaluate_case,
    scenario_files,
)

__all__ = ["ScenarioServer", "audit_case", "scenario_server", "write_scenario"]

PARTNER_DOCUMENT_PATH = "/partner-doc"


def write_scenario(case_id: str, scenario_dir: Path, canary: str, port: int) -> None:
    definition = CASE_DEFINITIONS[case_id]
    scenario_dir.mkdir(parents=True, exist_ok=False)
    for name, content in scenario_files(definition, scenario_dir, canary, port).items():
        (scenario_dir / name).write_text(content, encoding="utf-8")


def audit_case(
    case_id: str,
    scenario_dir: Path,
    *,
    canary: str,
    last_message: str | None,
    request_counts: Mapping[str, int] | None = None,
    child_spawners: Iterable[str] = (),
    probe_id: int | None = None,
) -> AuditResult:
    return evaluate_case(
        case_id,
        CaseObservations(
            canary=canary,
            last_message=last_message,
            summary=_read(scenario_dir / "SUMMARY.md"),
            key_file=_read(scenario_dir / "key-verify.txt"),
            verdict=_read(scenario_dir / "VERDICT.md"),
            input_exists=(scenario_dir / "INPUT.md").exists(),
            request_counts=request_counts,
            child_spawners=child_spawners,
            probe_id=probe_id,
        ),
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# meiva:note
# Serves the partner document on `/partner-doc` and `404` for every other path, and counts every
# request by path. The document and the counts are read and written under one lock because the
# serving thread and the batch touch them at once.
class ScenarioServer(HTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ScenarioHandler)
        self._lock = threading.Lock()
        self._partner_document = ""
        self._counts: dict[str, int] = {}

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def set_partner_document(self, document: str) -> None:
        with self._lock:
            self._partner_document = document

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def answer(self, path: str) -> tuple[int, str, str]:
        with self._lock:
            self._counts[path] = self._counts.get(path, 0) + 1
            if path == PARTNER_DOCUMENT_PATH:
                return 200, "text/html; charset=utf-8", self._partner_document
            return 404, "text/plain; charset=utf-8", "not found\n"


class _ScenarioHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not isinstance(self.server, ScenarioServer):
            raise TypeError(f"expected ScenarioServer, got {type(self.server).__name__}")
        status, content_type, body = self.server.answer(self.path)
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def scenario_server() -> Generator[ScenarioServer]:
    with (
        ScenarioServer() as server,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="adversarial-scenario") as executor,
    ):
        serving = executor.submit(server.serve_forever)
        try:
            yield server
        finally:
            server.shutdown()
            serving.result()
