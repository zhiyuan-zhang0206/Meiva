import socket
from pathlib import Path

import pytest

from base.config import ConfigBoot
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from services.derived.memory_indexer import daemon
from services.derived.memory_indexer.config import MemoryIndexerConfig
from services.derived.memory_search.tests.memory_indexer.test_reconcile_embed_error_truncates_and_returns_false import (
    owned_config_environment as owned_config_environment,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.usefixtures("owned_config_environment")
async def test_run_unknown_provider_fails_before_health_server(
    capsys: pytest.CaptureFixture[str], database: Database, tmp_path: Path
) -> None:
    health_port = _free_port()
    boot = ConfigBoot()
    boot.set_field("embedding_backend", "unknown-provider")
    boot.set_field("memory_indexer_health_port", health_port)

    with pytest.raises(SystemExit) as exc:
        await daemon.run(
            runtime=tmp_path / "run",
            config=boot,
            database=lambda: database,
            image=LoadedCommit(Path(), None),
            root=tmp_path / "memory",
            tuning=MemoryIndexerConfig(),
        )

    assert exc.value.code == 1
    assert "FATAL: unknown embedding provider 'unknown-provider'" in capsys.readouterr().err
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", health_port)) != 0, "the health port was bound"
