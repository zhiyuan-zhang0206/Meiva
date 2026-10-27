"""Operator database composition, without a process-wide exemption switch."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, NoReturn

from base.cluster.machine import MachineIdentityReader
from base.telemetry import DrainResult, EventPipeline

if TYPE_CHECKING:
    from base.cluster.dataplane.direct_url import ClusterDirectUrl
    from base.db import Database
    from base.db.code_version_gate import ProcessDbGate
    from base.db.config import DbConfig

__all__ = [
    "OpenDatabase",
    "OperatorDatabaseFactory",
    "OperatorEventPipeline",
    "open_operator_database",
    "operator_database_factory",
    "operator_event_pipeline",
]

# meiva:note
# `open_database(config, gate, direct_url)`: builds one handle. The types are imported for the
# checker only, because `meiva --help` must not load the configuration chain.
type OpenDatabase = Callable[[DbConfig, ProcessDbGate, ClusterDirectUrl], Database]


class OperatorDatabaseFactory:
    """One operator gate for normal handles and URL-specific recovery handles."""

    def __init__(
        self,
        *,
        local_host: Callable[[], str],
        home: Path,
        open_database: OpenDatabase,
    ) -> None:
        self._local_host = local_host
        self._home = home
        self._open_database = open_database
        self._lock = Lock()
        self._gate: ProcessDbGate | None = None
        self._adopted_url: str | None = None

    def adopt_login(self, db_url: str) -> None:
        """Dial `db_url` (the gateway login of a just-born or just-admitted generation) from now on.

        A start that births the database authority runs its boot before the ledger exists, so the
        process holds no login until the data-plane step has made one. Every handle this factory
        gives out afterwards, and the command's event writer built on it, dials the adopted URL;
        handles given out earlier keep the URL they were built with.

        The transition happens once: adopting the URL already adopted is a no-op, a different one
        raises, because a process whose login changes twice has lost track of which generation it
        acts as."""
        if self._adopted_url is not None and self._adopted_url != db_url:
            raise RuntimeError("this operator process already adopted a different gateway login")
        self._adopted_url = db_url

    def db_url(self) -> str:
        """The access URL a handle from this factory dials: the adopted login, else the settings'."""
        return self._config().db_url

    def _config(self) -> DbConfig:
        from base.db.config import db_config_from_settings

        config = db_config_from_settings()
        if self._adopted_url is None:
            return config
        return replace(config, db_url=self._adopted_url)

    def _admission_gate(self) -> ProcessDbGate:
        from base.db.code_version_gate import ProcessDbGate

        with self._lock:
            if self._gate is None:
                self._gate = ProcessDbGate(version=_unavailable_version, process="cli", exempt=True)
        return self._gate

    def __call__(self) -> Database:
        return self._open(self._config())

    def for_url(self, url: str) -> Database:
        return self._open(replace(self._config(), db_url=url))

    def _open(self, config: DbConfig) -> Database:
        from base.cluster.dataplane.direct_url import ClusterDirectUrl

        return self._open_database(
            config,
            self._admission_gate(),
            ClusterDirectUrl(self._local_host, home=self._home),
        )


def _unavailable_version() -> NoReturn:
    raise RuntimeError("an exempt operator database must not resolve a code version")


def open_operator_database(
    config: DbConfig, gate: ProcessDbGate, direct_url: ClusterDirectUrl
) -> Database:
    """The real handle: a `Database` over `config`, admitted by the operator `gate`."""
    from base.db import Database

    return Database(config, gate=gate, direct_url=direct_url)


def operator_database_factory(
    *,
    local_host: Callable[[], str],
    home: Path,
    open_database: OpenDatabase,
) -> OperatorDatabaseFactory:
    """Construct without configuration or Git; each handle reads live settings."""
    return OperatorDatabaseFactory(local_host=local_host, home=home, open_database=open_database)


class OperatorEventPipeline:
    """Retain one lazy command writer and its finite shutdown result."""

    def __init__(
        self,
        database_factory: OperatorDatabaseFactory,
        *,
        machine: MachineIdentityReader,
        home: Path,
        flush_interval_s: float = 0.5,
    ) -> None:
        self._machine = machine
        self._home = home
        self._flush_interval_s = flush_interval_s
        self._database_factory = database_factory
        self._pipeline: EventPipeline | None = None
        self._attach_process_logging = True
        self.log_sinks: list[str] = []

    # meiva:note
    # Bind an already-built `EventPipeline` to this command owner.
    @classmethod
    def wrapping(
        cls,
        pipeline: EventPipeline,
        *,
        database_factory: OperatorDatabaseFactory,
        machine: MachineIdentityReader,
        home: Path,
    ) -> OperatorEventPipeline:
        owner = cls(database_factory, machine=machine, home=home)
        owner._pipeline = pipeline
        owner._attach_process_logging = False
        return owner

    # meiva:note
    # Record `initialize_logging` sink names into `log_sinks` without opening process-wide sinks.
    @classmethod
    def recording(
        cls,
        database_factory: OperatorDatabaseFactory,
        *,
        machine: MachineIdentityReader,
        home: Path,
        log_sinks: list[str],
    ) -> OperatorEventPipeline:
        owner = cls(database_factory, machine=machine, home=home)
        owner._attach_process_logging = False
        owner.log_sinks = log_sinks
        return owner

    def __call__(self) -> EventPipeline:
        if self._pipeline is None:
            from base.telemetry import build_pipeline

            self._pipeline = build_pipeline(
                database=self._database_factory,
                machine=self._machine,
                home=self._home,
                flush_interval_s=self._flush_interval_s,
            )
        return self._pipeline

    def initialize_logging(self, name: str) -> None:
        """Bind logging to this command's lazy writer and identity reader."""
        self.log_sinks.append(name)
        if not self._attach_process_logging:
            return
        from base.telemetry.process_logging import init_cli_process

        init_cli_process(
            name=name, producer=self, machine_reader=self._machine.name, home=self._home
        )

    def close_command(self, *, primary: BaseException | None) -> None:
        """Report unfinished shutdown and preserve the command's primary error."""
        try:
            result = self.close(timeout=5.0)
            if result is not None:
                from base.telemetry import DrainStatus

                if result.status is DrainStatus.UNFINISHED:
                    print(
                        "meiva: command telemetry writer did not finish before exit",
                        file=sys.stderr,
                    )
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"command telemetry shutdown failed: {cleanup!r}")

    def close(self, timeout: float = 5.0) -> DrainResult | None:
        """Stop only an existing writer; retain unfinished handles and errors."""
        if self._pipeline is None:
            return None
        return self._pipeline.stop(timeout=timeout)


def operator_event_pipeline(
    database_factory: OperatorDatabaseFactory, machine: MachineIdentityReader, home: Path
) -> OperatorEventPipeline:
    """Construct a lazy command owner without opening a writer or database."""
    return OperatorEventPipeline(database_factory, machine=machine, home=home)
