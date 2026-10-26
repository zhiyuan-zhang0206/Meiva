from __future__ import annotations

import threading
from functools import partial
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base import db
from base.config import get_field, set_field
from base.db import DbConfig, connections
from base.db.code_version_gate import ProcessDbGate
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL

_PROCESS = "db-test"


def _direct_url_x(_config: object = None, **_kwargs: object) -> connections.DirectResolution:
    return connections.DirectResolution(url="postgresql://u:p@db:5432/x", is_direct=True)


def _direct_url_test(_config: object = None, **_kwargs: object) -> connections.DirectResolution:
    return connections.DirectResolution(url="postgresql://direct-test", is_direct=True)


def _cfg(
    db_url: str,
    *,
    db_sslmode: str = "",
    db_pool_min_size: int = 1,
    db_pool_max_size: int = 2,
    pgbouncer_enabled: bool = False,
) -> DbConfig:
    return DbConfig(
        db_url=db_url,
        db_sslmode=db_sslmode,
        db_pool_min_size=db_pool_min_size,
        db_pool_max_size=db_pool_max_size,
        pgbouncer_enabled=pgbouncer_enabled,
    )


def test_connect_refuses_placeholder_url(
    database_gate: ProcessDbGate,
) -> None:
    cfg = _cfg(PLACEHOLDER_DB_URL)
    with pytest.raises(db.PlaceholderDbUrlError):
        db.connect(config=cfg, gate=database_gate)


def test_pool_refuses_placeholder_url(
    database_gate: ProcessDbGate,
) -> None:
    cfg = _cfg(PLACEHOLDER_DB_URL)
    with pytest.raises(db.PlaceholderDbUrlError):
        db.pool(config=cfg, gate=database_gate)


def test_async_pool_refuses_placeholder_url(
    database_gate: ProcessDbGate,
) -> None:
    cfg = _cfg(PLACEHOLDER_DB_URL)
    with pytest.raises(db.PlaceholderDbUrlError):
        db.async_pool(
            AsyncConnectionPool,
            min_size=0,
            max_size=1,
            timeout=1.0,
            config=cfg,
            gate=database_gate,
        )


def test_async_pool_fixes_the_transport_posture(
    database_gate: ProcessDbGate,
) -> None:
    # meiva:note
    # Read psycopg_pool internal fields via getattr to verify pool constructor wiring without opening sockets.
    cfg = _cfg("postgresql://u@127.0.0.1:1/x", db_sslmode="require")
    database_gate = ProcessDbGate(version=lambda: 7, process=_PROCESS)
    pool = db.async_pool(
        AsyncConnectionPool,
        name="probe",
        min_size=0,
        max_size=3,
        timeout=2.0,
        config=cfg,
        gate=database_gate,
    )
    check = getattr(pool, "_check")  # noqa: B009
    assert isinstance(check, partial)
    assert check.func is connections.restore_pooled_session_async
    assert check.keywords == {"gate": database_gate}
    assert pool.conninfo == "postgresql://u@127.0.0.1:1/x"
    assert pool.min_size == 0
    assert pool.max_size == 3
    assert pool.timeout == 2.0
    assert getattr(pool, "_open_implicit") is False  # noqa: B009
    assert getattr(pool, "_opened") is False  # noqa: B009
    assert pool.name == "probe"
    assert pool.kwargs == {
        "autocommit": True,
        "prepare_threshold": None,
        "sslmode": "require",
        "application_name": f"meiva:{_PROCESS}:v7",
        **db.PG_KEEPALIVE_KWARGS,
    }


def _spy_dials(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    dials: list[tuple[str, dict[str, Any]]] = []
    owner = threading.current_thread()

    def spy(conninfo: str = "", **kwargs: Any) -> object:
        if threading.current_thread() is owner:
            dials.append((conninfo, kwargs))
        return object()

    monkeypatch.setattr(psycopg, "connect", spy)
    return dials


def test_connect_url_owns_the_transport_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    old_sslmode = get_field("db_sslmode")
    set_field("db_sslmode", "require")
    try:
        dials = _spy_dials(monkeypatch)
        url = "postgresql://owner@/meiva?host=/tmp/sock&options=-c%20role%3Dmeiva"
        db.connect_url(url, autocommit=True)
    finally:
        set_field("db_sslmode", old_sslmode)
    assert dials == [
        (
            url,
            {
                "autocommit": True,
                "prepare_threshold": None,
                **db.PG_KEEPALIVE_KWARGS,
                "options": "-c role=meiva -c statement_timeout=60000",
            },
        )
    ]


def test_connect_url_unbounded_keeps_the_keepalives(monkeypatch: pytest.MonkeyPatch) -> None:
    dials = _spy_dials(monkeypatch)
    db.connect_url("postgresql://u@127.0.0.1:1/x", unbounded=True, connect_timeout=2)
    assert dials == [
        (
            "postgresql://u@127.0.0.1:1/x",
            {
                "autocommit": False,
                "prepare_threshold": None,
                **db.PG_KEEPALIVE_KWARGS,
                "connect_timeout": 2,
            },
        )
    ]


def test_connect_unbounded_keeps_the_keepalives(
    database_gate: ProcessDbGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg("postgresql://u:p@db.example:5432/x", db_sslmode="")
    dials = _spy_dials(monkeypatch)
    db.connect(
        direct=True,
        unbounded=True,
        config=cfg,
        gate=database_gate,
        direct_url=_direct_url_x,
    )
    assert dials == [
        (
            "postgresql://u:p@db:5432/x",
            {"autocommit": False, "prepare_threshold": None, **db.PG_KEEPALIVE_KWARGS},
        )
    ]


def test_connect_url_refuses_placeholder_url() -> None:
    with pytest.raises(db.PlaceholderDbUrlError):
        db.connect_url(PLACEHOLDER_DB_URL)


def test_pool_check_connections_flag(
    database_gate: ProcessDbGate,
) -> None:
    # meiva:note
    # Read psycopg_pool internal fields via getattr to verify check/configure callbacks on constructed pools.
    real_check = connections.ConnectionPool.check_connection
    cfg = _cfg("postgresql://pooled-test", db_pool_min_size=0, db_pool_max_size=1)
    with db.pool(config=cfg, gate=database_gate) as pooled:
        configure = getattr(pooled, "_configure")  # noqa: B009
        check = getattr(pooled, "_check")  # noqa: B009
        assert isinstance(configure, partial) and isinstance(check, partial)
        assert configure.func is connections.restore_pooled_session
        assert check.func is connections.restore_pooled_session
        assert configure.keywords == check.keywords == {"gate": database_gate}
    with db.pool(
        direct=True,
        check_connections=True,
        config=cfg,
        gate=database_gate,
        direct_url=_direct_url_test,
    ) as direct_checked:
        assert getattr(direct_checked, "_configure") is None  # noqa: B009
        assert getattr(direct_checked, "_check") is real_check  # noqa: B009
    with db.pool(
        direct=True,
        config=cfg,
        gate=database_gate,
        direct_url=_direct_url_test,
    ) as direct_unchecked:
        assert getattr(direct_unchecked, "_check") is None  # noqa: B009
