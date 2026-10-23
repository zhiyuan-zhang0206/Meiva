"""The standalone CI samplers' database gate and finite event-writer lifetime."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.log import logger
from base.native_process.code_version import CodeVersion, CodeVersionError, first_parent_count
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry import DrainStatus, EventPipeline, build_pipeline

__all__ = ["owned_event_pipeline"]


def _code_version_getter(
    image: LoadedCommit, count_fn: Callable[[Path, str], int]
) -> Callable[[], int]:
    if count_fn is first_parent_count:
        return CodeVersion(image).get
    cached: int | None = None

    def get() -> int:
        nonlocal cached
        if cached is None:
            if image.sha is None:
                raise CodeVersionError(f"cannot resolve the loaded commit of {image.source_root}")
            cached = count_fn(image.source_root, image.sha)
        return cached

    return get


@contextmanager
def owned_event_pipeline(
    process: str,
    *,
    image: LoadedCommit | None,
    boot_cls: Callable[[], ConfigBoot] = ConfigBoot,
    home_fn: Callable[[], Path] = meiva_home,
    db_from_settings: Callable[..., Database] = Database.from_settings,
    pipeline_builder: Callable[..., EventPipeline] = build_pipeline,
    count_fn: Callable[[Path, str], int] = first_parent_count,
) -> Generator[EventPipeline]:
    """Own the sampler's actual writer; a scheduled caller supplies its existing producer."""
    if image is None:
        raise ValueError("a standalone exporter requires its entry's loaded image")
    gate = ProcessDbGate(process=process, version=_code_version_getter(image, count_fn))

    def database() -> Database:
        return db_from_settings(
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    config = boot_cls()
    machine = machine_from_boot(config)
    pipeline = pipeline_builder(database=database, machine=machine, home=home_fn())
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
