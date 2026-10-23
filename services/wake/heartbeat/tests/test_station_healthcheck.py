"""`services.wake.heartbeat.station_probe` unit tests — the remote-station probe.

WP4 (task #1946): the gateway probes a REMOTE observatory station through
the reachability contract (the address the station advertises in
machine_units) with the cluster bearer, and FAILS OPEN — a failed probe
never blocks or restarts local business; a failed probe only logs. With no
MEIVA_OBSERVABILITY_URL configured the check is a no-op.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import base.db
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.db_authority.api import (
    publish_telemetry_token,
    read_telemetry_token,
    telemetry_token,
    telemetry_token_path,
)
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from services.wake.heartbeat import station_probe as hc


def _deps(**overrides: Any) -> hc.MainDeps:
    base = hc.MainDeps(
        init_gateway_process=Mock(),
        build_pipeline=Mock(return_value=object()),
        observability_url="",
    )
    return replace(base, **overrides)


class _FakeCodeVersion(CodeVersion):
    def __init__(self, loaded: LoadedCommit, *, count_mock: Mock) -> None:
        super().__init__(loaded)
        self._count_mock = count_mock

    def get(self) -> int:
        if self._version is None:
            if self.loaded.sha is None:
                self._version = 0
            else:
                count = int(self._count_mock(self.loaded.source_root, self.loaded.sha))
                self._version = max(count, 0)
        return self._version


@pytest.fixture(autouse=True)
def _clean_state(*, db_url: str, database_gate: ProcessDbGate) -> None:
    """Reset the in-process probe state between tests (the module dict is
    process-global, same as the watchdog would hold it)."""
    with base.db.connect(gate=database_gate) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM machine_units WHERE machine_name LIKE 'station-test%'")
        conn.commit()


@pytest.fixture(autouse=True)
def _no_remote_observatory() -> None:
    """Default: publish telemetry token for tests."""
    publish_telemetry_token(meiva_home(), "cluster-token")


def _insert_station_unit(
    name: str = "station-test-a", url: str = "http://10.0.0.9:4318", *, database_gate: ProcessDbGate
) -> None:
    with base.db.connect(gate=database_gate) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_units "
            "(machine_name, home, serve_gateway, serve_agent_runner, "
            "serve_observability_station, url, up_since_at, stopped_at) "
            "VALUES (%s, %s, false, false, true, %s, now(), NULL)",
            (name, "~/.meiva_station", url),
        )
        conn.commit()


def test_main_is_noop_without_remote_observatory() -> None:
    """No MEIVA_OBSERVABILITY_URL -> nothing to probe: no DB read, no network,
    no alert (the local observatory is the lgtm check's job)."""
    calls: list[str] = []

    def _fail_if_called(url: str) -> bool:
        calls.append(url)
        return False

    create = Mock(side_effect=AssertionError("a no-op probe must not open its work database"))
    hc.main(probe=_fail_if_called, deps=_deps(database_from_settings=create))
    assert calls == []
    create.assert_not_called()


def test_resolve_target_uses_advertised_machine_units_url(
    database: Database, *, database_gate: ProcessDbGate
) -> None:
    """The probe target is the station's ADVERTISED machine_units url — the
    reachability contract — not the configured base."""
    _insert_station_unit(database_gate=database_gate)
    target = hc.resolve_target(database=lambda: database, observability_url="http://10.0.0.9")
    assert target is not None
    assert target.url == "http://10.0.0.9:4318"
    assert target.advertised is True
    assert target.name == "station-test-a"


def test_resolve_target_falls_back_to_configured_base_without_registration(
    database: Database,
) -> None:
    """MEIVA_OBSERVABILITY_URL set but no station unit registered -> warn loudly
    and probe the configured base + OTLP port (fail-open: still give signal)."""
    target = hc.resolve_target(database=lambda: database, observability_url="http://10.0.0.46")
    assert target is not None
    assert target.url == "http://10.0.0.46:4318"
    assert target.advertised is False


def test_resolve_target_skips_hybrid_gateway_station_units(
    database: Database, *, database_gate: ProcessDbGate
) -> None:
    """A hybrid gateway+station unit advertises its GATEWAY url (unit_dial_url
    lets the gateway capability win), not the OTLP ingress — probing it would
    hit the gateway API and alert forever (QA #1156 NIT-2). Only units whose
    capability set identifies an ingress advertisement qualify."""
    _insert_station_unit(url="http://10.0.0.46:4318", database_gate=database_gate)
    with base.db.connect(gate=database_gate) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machine_units SET serve_gateway = true WHERE machine_name = 'station-test-a'"
        )
        conn.commit()
    target = hc.resolve_target(database=lambda: database, observability_url="http://10.0.0.46")
    assert target is not None
    assert target.url == "http://10.0.0.46:4318"
    assert target.advertised is False


def test_resolve_target_none_without_observability_url(database: Database) -> None:
    assert hc.resolve_target(database=lambda: database, observability_url="") is None


def test_station_answers_bearer_otlp_roundtrip() -> None:
    """A 2xx on POST <url>/v1/traces with the telemetry bearer = alive."""
    seen: dict[str, object] = {}

    class _Resp:
        status = 200

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def _open(req: urllib.request.Request, **kw: object) -> _Resp:
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return _Resp()

    assert hc.station_answers("http://10.0.0.9:4318", urlopen=_open) is True
    assert seen["url"] == "http://10.0.0.9:4318/v1/traces"
    # The cluster's telemetry token from the file the start published, never the secret.
    assert seen["auth"] == f"Bearer {telemetry_token('cluster-token')}"


def test_station_answers_http_error_is_not_alive() -> None:
    """A non-2xx answer (401 auth, 415 body, 5xx) proves the listener is up
    but the ingestion path is not serving — not alive."""

    def _raise(_req: object, **_kw: object) -> None:
        raise urllib.error.HTTPError("http://x/v1/traces", 401, "unauthorized", {}, None)  # pyright: ignore[reportArgumentType]

    assert hc.station_answers("http://10.0.0.9:4318", urlopen=_raise) is False


def test_station_answers_connection_failure_is_not_alive(
    loguru_records: list[dict[str, Any]],
) -> None:
    def _raise(_req: object, **_kw: object) -> None:
        raise OSError("connection refused")

    assert hc.station_answers("http://10.0.0.9:4318", urlopen=_raise) is False
    record = next(r for r in loguru_records if "unreachable" in r["message"])
    assert record["exception"] is not None
    assert record["exception"].type is OSError


def test_station_answers_without_a_published_token_warns_and_fails_open(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No published telemetry token -> cannot authenticate the probe; warn and treat as
    alive rather than alerting on a config problem (fail-open)."""
    telemetry_token_path(meiva_home()).unlink()  # a start that has not published yet
    assert read_telemetry_token(meiva_home()) is None

    def _fail_if_called(_req: object, **_kw: object) -> None:
        pytest.fail("must not dial without a token")

    assert hc.station_answers("http://10.0.0.9:4318", urlopen=_fail_if_called) is True


def test_main_probes_advertised_station_and_does_not_raise(*, database_gate: ProcessDbGate) -> None:
    """End-to-end: with a registered station, main() runs clean on a healthy
    probe and fails open on a failing one."""
    _insert_station_unit(database_gate=database_gate)
    answers = [True, False]

    def probe(_url: str) -> bool:
        return answers.pop(0)

    deps = _deps(observability_url="http://10.0.0.9")
    hc.main(probe=probe, deps=deps)
    hc.main(probe=probe, deps=deps)  # must not raise
    assert answers == []


def test_main_shares_one_captured_gate_with_the_probe_and_log_factory(
    tmp_path: Path,
) -> None:
    image = LoadedCommit(tmp_path, "captured-before-checkout-moved")
    capture = Mock(return_value=image)
    count = Mock(return_value=7)
    create = Mock(return_value=Mock(spec=Database))
    target = Mock(url="http://station.invalid:4318")
    received: list[object] = []

    def resolve(*, database: Callable[[], Database]) -> Any:
        received.append(database)
        database()
        return target

    def code_version_factory(loaded: LoadedCommit) -> CodeVersion:
        return _FakeCodeVersion(loaded, count_mock=count)

    logging = Mock()
    pipeline = Mock(return_value=object())
    hc.main(
        probe=Mock(return_value=True),
        deps=hc.MainDeps(
            capture_image=capture,
            code_version_cls=code_version_factory,
            database_from_settings=create,
            resolve_target=resolve,
            build_pipeline=pipeline,
            init_gateway_process=logging,
        ),
    )

    capture.assert_called_once_with()
    count.assert_not_called()
    factory = pipeline.call_args.kwargs["database"]
    assert received == [factory]
    assert logging.call_args.kwargs["image"] is image
    assert logging.call_args.kwargs["producer"]() is pipeline.return_value
    factory()
    gates = [call.kwargs["gate"] for call in create.call_args_list]
    assert len(gates) == 2 and gates[0] is gates[1]
    assert isinstance(gates[0], ProcessDbGate)
    assert gates[0].application_name() == "meiva:station:v7"
    count.assert_called_once_with(tmp_path, image.sha)
    gates[0].observe_minimum(7)
    assert not gates[0].min_read_due()


def test_station_lookup_keeps_the_original_fail_open_boundary() -> None:
    create = Mock(side_effect=RuntimeError("original database failure"))
    assert hc.resolve_target(database=create, observability_url="http://station.invalid") is None
    create.assert_called_once_with()
