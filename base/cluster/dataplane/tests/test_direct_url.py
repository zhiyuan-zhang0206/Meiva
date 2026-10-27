from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import cast

import pytest

from base.cluster.dataplane.direct_url import ClusterDirectUrl, resolve_direct_url
from base.cluster.ports import ClusterPorts
from base.cluster.record import INTENT_NAME, ClusterRecord
from base.config import DataPlaneSettings
from base.db import Database, DbConfig
from base.db.code_version_gate import ProcessDbGate
from base.db.connections import DirectResolution, DirectUnavailableError
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.paths import meiva_home

_POOLED = "postgresql://meiva_main:sek@127.0.0.1:6433/meiva_main"
_DIRECT = "postgresql://meiva_main:sek@127.0.0.1:5433/meiva_main"
_HOME = "/x/.meiva-t"


def test_pgbouncer_enabled_defaults_on() -> None:
    assert DataPlaneSettings.model_fields["pgbouncer_enabled"].default is True


def test_no_pgbouncer_port_field_on_the_settings_surface() -> None:
    assert "pgbouncer_port" not in DataPlaneSettings.model_fields


def _rec(home: str, ports: dict[str, int]) -> ClusterRecord:
    return ClusterRecord(ports=cast("ClusterPorts", ports), gateway_home=home, created_at="t")


_PG_REC = _rec(_HOME, {"gateway": 8000, "postgres": 5433, "redis": 6380, "pgbouncer": 6433})


def _serve_record(
    home: Path,
    rec: ClusterRecord | None = None,
    *,
    must_not_be_read: bool = False,
) -> Path:
    intent = home / INTENT_NAME
    if must_not_be_read:
        intent.write_bytes(b"\xff")
    elif rec is None:
        intent.unlink(missing_ok=True)
    else:
        intent.write_text(json.dumps({"record": dataclasses.asdict(rec)}))
    return home


def _config(url: str, *, pooling: bool = True) -> DbConfig:
    return DbConfig(
        db_url=url,
        db_sslmode="",
        db_pool_min_size=1,
        db_pool_max_size=2,
        pgbouncer_enabled=pooling,
    )


def _unused_reader() -> str:
    raise AssertionError("this resolution must not read the dependency")


def test_direct_handle_uses_the_owned_lazy_host_reader_and_preserves_short_circuits(
    database_gate: ProcessDbGate,
    tmp_path: Path,
) -> None:
    home = _serve_record(tmp_path, _PG_REC)
    reads: list[str] = []

    def local_host() -> str:
        reads.append("host")
        return "OWNED-HOST"

    def handle(url: str) -> Database:
        return Database(
            _config(url),
            direct_url=ClusterDirectUrl(local_host, home=home),
            gate=database_gate,
        )

    assert handle(PLACEHOLDER_DB_URL).direct_url() == PLACEHOLDER_DB_URL
    assert handle("not-a-port").direct_url() == "not-a-port"
    assert handle(_POOLED).direct_url() == _DIRECT
    assert reads == []
    assert handle(_POOLED.replace("127.0.0.1", "owned-host")).direct_url() == _DIRECT.replace(
        "127.0.0.1", "owned-host"
    )
    assert reads == ["host"]


@pytest.mark.parametrize(
    "url",
    [
        PLACEHOLDER_DB_URL,
        "postgresql://host:bad/meiva",
        "postgresql://host/meiva",
        "postgresql://[/meiva",
    ],
)
def test_a_url_that_cannot_name_a_pooler_is_direct_and_reads_no_dependency(
    url: str, tmp_path: Path
) -> None:
    home = _serve_record(tmp_path, must_not_be_read=True)
    resolution = resolve_direct_url(_config(url), local_host=_unused_reader, home=home)
    assert resolution == DirectResolution(url=url, is_direct=True)


def test_pooling_off_makes_the_access_url_direct_without_reading_anything(
    tmp_path: Path,
) -> None:
    home = _serve_record(tmp_path, must_not_be_read=True)
    url = "postgresql://meiva:dev@db.example:5432/meiva"
    resolution = resolve_direct_url(
        _config(url, pooling=False), local_host=_unused_reader, home=home
    )
    assert resolution == DirectResolution(url=url, is_direct=True)


def test_host_comparison_requires_an_explicit_reader() -> None:
    with pytest.raises(RuntimeError, match="explicit local_host reader"):
        resolve_direct_url(_config(_POOLED.replace("127.0.0.1", "owned-host")), home=meiva_home())


@pytest.mark.parametrize("url, expected", [(_POOLED, _DIRECT), (_DIRECT, _DIRECT)])
def test_a_local_record_resolves_the_port_it_names(tmp_path: Path, url: str, expected: str) -> None:
    home = _serve_record(tmp_path, _PG_REC)
    assert resolve_direct_url(
        _config(url), local_host=_unused_reader, home=home
    ) == DirectResolution(url=expected, is_direct=True)


@pytest.mark.parametrize("pg_port, pooler_port", [(18011, 18013), (19011, 19013)])
def test_direct_url_uses_the_ports_the_record_names(
    tmp_path: Path, pg_port: int, pooler_port: int
) -> None:
    rec = _rec(
        "/x/.meiva-other",
        {"gateway": 18000, "postgres": pg_port, "redis": 18012, "pgbouncer": pooler_port},
    )
    home = _serve_record(tmp_path, rec)
    url = f"postgresql://meiva:sek@127.0.0.1:{pooler_port}/meiva?sslmode=prefer"
    assert resolve_direct_url(_config(url), local_host=_unused_reader, home=home).url == (
        f"postgresql://meiva:sek@127.0.0.1:{pg_port}/meiva?sslmode=prefer"
    )


def test_a_local_port_the_record_does_not_name_is_not_direct(
    tmp_path: Path,
) -> None:
    home = _serve_record(tmp_path, _PG_REC)
    url = "postgresql://meiva:dev@localhost:5000/meiva"
    resolution = resolve_direct_url(_config(url), local_host=_unused_reader, home=home)
    assert resolution.url == url
    assert resolution.is_direct is False
    assert "localhost:5000" in resolution.reason
    assert "PgBouncer port 6433" in resolution.reason
    assert "Postgres port 5433" in resolution.reason


def test_a_home_without_a_record_cannot_tell_whether_the_port_is_a_pooler(
    tmp_path: Path,
) -> None:
    home = _serve_record(tmp_path)
    resolution = resolve_direct_url(_config(_POOLED), local_host=_unused_reader, home=home)
    assert resolution.url == _POOLED
    assert resolution.is_direct is False
    assert "127.0.0.1:6433" in resolution.reason
    assert "no cluster record" in resolution.reason


@pytest.mark.parametrize("host", ["10.0.0.9", "DB.EXAMPLE", "[2001:db8::9]"])
def test_a_foreign_host_is_never_resolved_here(tmp_path: Path, host: str) -> None:
    home = _serve_record(tmp_path, _PG_REC)
    url = f"postgresql://meiva_main:sek@{host}:6433/meiva_main"
    resolution = ClusterDirectUrl(lambda: "owned-host", home=home)(_config(url))
    assert resolution.url == url
    assert resolution.is_direct is False
    assert "which is another machine" in resolution.reason
    assert "6433" in resolution.reason


def test_the_handle_refuses_a_direct_dial_it_cannot_resolve(
    database_gate: ProcessDbGate, tmp_path: Path
) -> None:
    home = _serve_record(tmp_path)
    handle = Database(
        _config(_POOLED), direct_url=ClusterDirectUrl(_unused_reader, home=home), gate=database_gate
    )
    with pytest.raises(DirectUnavailableError) as caught:
        handle.direct_url()
    assert "cannot open a direct (non-pooled) database connection" in str(caught.value)
    assert "127.0.0.1:6433" in str(caught.value)
    assert handle.direct_resolution().url == _POOLED


def test_every_refused_resolution_carries_a_reason(tmp_path: Path) -> None:
    cases: list[DirectResolution] = []
    home = _serve_record(tmp_path)
    cases.append(resolve_direct_url(_config(_POOLED), local_host=_unused_reader, home=home))
    home = _serve_record(tmp_path, _PG_REC)
    cases.append(
        resolve_direct_url(
            _config("postgresql://127.0.0.1:5000/m"), local_host=_unused_reader, home=home
        )
    )
    cases.append(
        ClusterDirectUrl(lambda: "owned-host", home=home)(_config("postgresql://10.0.0.9:6433/m"))
    )
    for resolution in cases:
        assert resolution.is_direct is False
        assert resolution.reason
    assert len({resolution.reason for resolution in cases}) == 3


def test_the_resolution_keeps_the_login_out_of_its_repr(tmp_path: Path) -> None:
    home = _serve_record(tmp_path, _PG_REC)
    resolution = resolve_direct_url(_config(_POOLED), local_host=_unused_reader, home=home)
    assert "sek" not in repr(resolution)
    assert resolution.url == _DIRECT
