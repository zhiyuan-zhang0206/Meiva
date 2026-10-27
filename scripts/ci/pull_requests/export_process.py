"""The standalone CI samplers' database gate and finite event-writer lifetime."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import DrainStatus, EventPipeline, build_pipeline

__all__ = ["drain_event_pipeline", "owned_event_pipeline", "process_gate"]


def process_gate(process: str, *, image: LoadedCommit | None) -> ProcessDbGate:
    if image is None:
        raise ValueError("a standalone exporter requires its entry's loaded image")
    version = CodeVersion(image)
    return ProcessDbGate(process=process, version=version.get)


@contextmanager
def drain_event_pipeline(process: str, pipeline: EventPipeline) -> Generator[EventPipeline]:
    primary: BaseException | None = None
    try:
        yield pipeline
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            receipt = pipeline.stop(timeout=2)
            if receipt.status is DrainStatus.UNFINISHED:
                logger.error("{} event writer remains unfinished after its drain budget", process)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"exporter event writer cleanup failed: {cleanup!r}")


@contextmanager
def owned_event_pipeline(
    process: str, *, image: LoadedCommit | None, home: Path
) -> Generator[EventPipeline]:
    """Own the sampler's actual writer; a scheduled caller supplies its existing producer."""
    gate = process_gate(process, image=image)

    def database() -> Database:
        return Database.from_settings(
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )

    config = ConfigBoot()
    machine = ConfiguredMachineIdentity(config)
    pipeline = build_pipeline(database=database, machine=machine, home=home)
    with drain_event_pipeline(process, pipeline):
        yield pipeline
