# meiva:note
# Fleet silence behavior against a loopback Grafana wire. Pure window facts use a fixed clock;
# the executable entry reads real private-home configuration in an isolated interpreter.

from __future__ import annotations

import base64
import json
import socket
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from base.host.proc import run_bounded
from cli import fleet_alert_silence as silence

_NOW = datetime(2026, 10, 4, 4, 0, tzinfo=UTC)


class FakeGrafana:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []

    def add_foreign(self) -> None:
        self.rows.append(
            {
                "id": "other",
                "createdBy": "someone-else",
                "status": {"state": "active"},
                "startsAt": "2026-10-04T03:00:00Z",
            }
        )

    def create(self, body: dict[str, Any]) -> str:
        known = next((row for row in self.rows if row["id"] == body.get("id")), None)
        if known is not None:
            known.update(body)
            return str(known["id"])
        row = {**body, "id": f"s{len(self.rows) + 1}", "status": {"state": "active"}}
        self.rows.append(row)
        return str(row["id"])


@pytest.fixture
def grafana() -> Iterator[tuple[FakeGrafana, str]]:
    fake = FakeGrafana()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: object) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            expected = "Basic " + base64.b64encode(b"admin:pw").decode()
            if self.headers.get("Authorization") == expected:
                return True
            self._reply(401, {})
            return False

        def do_GET(self) -> None:
            fake.calls.append(("GET", self.path))
            if self._authorized():
                self._reply(200, fake.rows)

        def do_POST(self) -> None:
            fake.calls.append(("POST", self.path))
            if self._authorized():
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self._reply(200, {"silenceID": fake.create(body)})

        def do_DELETE(self) -> None:
            fake.calls.append(("DELETE", self.path))
            if self._authorized():
                ident = self.path.rsplit("/", 1)[1]
                for row in fake.rows:
                    if row["id"] == ident:
                        row["status"] = {"state": "expired"}
                self._reply(200, {"message": "silence deleted"})

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_open_creates_one_silence_over_every_alert_but_a_full_disk_with_expiry_and_comment(
    grafana: tuple[FakeGrafana, str],
) -> None:
    fake, base = grafana
    ident = silence.open_window(base, "pw", hours=4, comment="planned", now=_NOW)
    (row,) = fake.rows
    assert row["id"] == ident
    assert row["matchers"] == [
        {"name": "alertname", "value": ".+", "isRegex": True, "isEqual": True},
        {"name": "metric", "value": "host_disk", "isRegex": False, "isEqual": False},
        {"name": "attributes_check", "value": "disk_usage", "isRegex": False, "isEqual": False},
    ]
    assert row["createdBy"] == silence.CREATED_BY and row["comment"] == "planned"
    assert row["startsAt"] == "2026-10-04T04:00:00+00:00"
    assert row["endsAt"] == (_NOW + timedelta(hours=4)).isoformat(timespec="seconds")


def test_a_rerun_extends_the_silence_it_owns_instead_of_stacking_another(
    grafana: tuple[FakeGrafana, str],
) -> None:
    fake, base = grafana
    first = silence.open_window(base, "pw", hours=4, comment="a", now=_NOW)
    second = silence.open_window(base, "pw", hours=6, comment="b", now=_NOW + timedelta(minutes=30))
    assert second == first and len(fake.rows) == 1
    assert fake.rows[0]["endsAt"] == (_NOW + timedelta(minutes=30, hours=6)).isoformat(
        timespec="seconds"
    )
    assert fake.rows[0]["startsAt"] == "2026-10-04T04:00:00+00:00"


def test_close_expires_only_the_silences_it_owns(grafana: tuple[FakeGrafana, str]) -> None:
    fake, base = grafana
    fake.add_foreign()
    owned = silence.open_window(base, "pw", hours=4, comment="a", now=_NOW)
    assert silence.close_window(base, "pw") == [owned]
    states = {row["id"]: row["status"]["state"] for row in fake.rows}
    assert states == {"other": "active", owned: "expired"}
    assert silence.close_window(base, "pw") == []


# meiva:note
# The entry reads a real .env in its private home in a fresh interpreter. HTTP and credential
# handling stay real, including the configured URL's normalization and an absent password.
def _run(home: Path, base: str, credential: str | None, *argv: str) -> str:
    values = {"MEIVA_TELEMETRY_GRAFANA_URL": base}
    if credential is not None:
        values["GRAFANA_ADMIN_PASSWORD"] = credential
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    env = {
        "HOME": str(home),
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
        "MEIVA_TIMEZONE": "UTC",
    }
    child = run_bounded(
        [sys.executable, "-I", "-m", "cli.fleet_alert_silence", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    return child.stdout


def test_main_opens_then_closes_through_the_hosts_settings(
    grafana: tuple[FakeGrafana, str], tmp_path: Path
) -> None:
    fake, base = grafana
    assert _run(tmp_path, base, "pw", "open", "--hours", "2", "--comment", "window").startswith(
        "SILENCE opened id=s1 until="
    )
    assert fake.rows[0]["status"]["state"] == "active"
    assert _run(tmp_path, base, "pw", "close") == "SILENCE closed 1\n"
    assert fake.rows[0]["status"]["state"] == "expired"


def test_an_unreachable_grafana_is_reported_not_raised(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as denied:
        denied.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{denied.getsockname()[1]}"
        output = _run(tmp_path, base, "pw", "open", "--hours", "1", "--comment", "w")
    assert output.startswith("SILENCE failed: GET /api/")


def test_a_refused_credential_is_reported_not_raised(
    grafana: tuple[FakeGrafana, str], tmp_path: Path
) -> None:
    _fake, base = grafana
    assert _run(tmp_path, base, "wrong", "close") == (
        f"SILENCE failed: GET {silence.SILENCES}: HTTP 401\n"
    )


@pytest.mark.parametrize("credential", [None, ""])
def test_a_home_without_the_admin_password_skips(
    grafana: tuple[FakeGrafana, str], tmp_path: Path, credential: str | None
) -> None:
    fake, base = grafana
    assert _run(tmp_path, base, credential, "close").startswith("SILENCE skipped:")
    assert fake.calls == []


def test_the_credential_comes_from_the_alerts_settings(
    grafana: tuple[FakeGrafana, str], tmp_path: Path
) -> None:
    fake, base = grafana
    assert _run(tmp_path, base + "/", "pw", "close") == "SILENCE closed 0\n"
    assert fake.calls == [("GET", silence.SILENCES)]
