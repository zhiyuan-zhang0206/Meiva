import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_config_images import (
    PluginConfigChangedError,
    PluginConfigOwner,
    image_revision,
    write_config_image,
)
from base.packages.plugins import config_registration
from base.packages.plugins.config_registration import (
    bind_plugin_config,
    disk_image_path,
    get_plugin_config,
)
from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError


class BootConfig(BaseModel):
    marker: str = "default"


def _run_boot_race_child(child_marker: str) -> None:
    from base.config import settings

    class Config(BaseModel):
        marker: str = child_marker

    original = config_registration.write_default_disk_image

    def synchronized_write(plugin: str, cls: type[BaseModel]) -> Path:
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        assert sys.stdin.readline() == "CREATE\n"
        return original(plugin, cls)

    configs: dict[str, BaseModel] = {}
    bind_plugin_config("boot-race", Config, configs, write_default_fn=synchronized_write)
    slices = AgentSlices.resolve(
        plugin_configs=configs,
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
    )
    bound = get_plugin_config("boot-race", slices, Config)
    sys.stdout.write(f"{bound.marker}\n")
    sys.stdout.flush()


async def _release_creation(
    children: list[asyncio.subprocess.Process],
) -> list[tuple[bytes, bytes]]:
    for child in children:
        assert child.stdin is not None
        child.stdin.write(b"CREATE\n")
        await child.stdin.drain()
    return await asyncio.wait_for(asyncio.gather(*(child.communicate() for child in children)), 30)


async def test_fresh_processes_bind_the_same_winning_image(unit_home: Path) -> None:
    children: list[asyncio.subprocess.Process] = []
    try:
        for marker in ("first", "second"):
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                marker,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            children.append(child)
        for child in children:
            assert child.stdout is not None
            assert await asyncio.wait_for(child.stdout.readline(), 30) == b"READY\n"
        assert not disk_image_path("boot-race").exists()
        results = await _release_creation(children)
        for child, (_, stderr) in zip(children, results, strict=True):
            assert child.returncode == 0, stderr.decode()
        winner = json.loads(disk_image_path("boot-race").read_text())["marker"]
        assert winner in ("first", "second")
        assert [stdout.decode().strip() for stdout, _ in results] == [winner, winner]
    finally:
        for child in children:
            if child.returncode is None:
                child.terminate()
                await child.wait()


def test_bind_rereads_image_created_after_the_missing_read(
    unit_home: Path,
) -> None:
    configs: dict[str, BaseModel] = {}
    reader = config_registration.read_plugin_config
    image = disk_image_path("read-race")

    def competing_read(plugin: str, cls: type[BaseModel], path: Path) -> BaseModel:
        instance = reader(plugin, cls, path)
        if not path.exists():
            write_config_image(
                PluginConfigOwner(plugin, cls, path),
                BootConfig(marker="winner"),
                expected_digest=image_revision(None),
            )
        return instance

    undo = bind_plugin_config(
        "read-race", BootConfig, configs, read_plugin_config_fn=competing_read
    )
    try:
        assert (
            get_plugin_config(
                "read-race",
                AgentSlices.resolve(
                    plugin_configs=configs,
                    default_reader=defaults(),
                ),
                BootConfig,
            ).marker
            == "winner"
        )
        assert json.loads(image.read_text()) == {"marker": "winner"}
    finally:
        undo()


@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ("", InvalidConfigData),
        ("invalid-json", InvalidConfigData),
        ('{"unexpected": 1}', SchemaDriftError),
        ('{"marker": 123}', InvalidConfigData),
    ],
)
def test_bind_rejects_invalid_winning_image(
    unit_home: Path,
    content: str,
    expected_error: type[Exception],
) -> None:
    configs: dict[str, BaseModel] = {}
    original = config_registration.write_default_disk_image
    image = disk_image_path("invalid-winner")

    def competing_write(plugin: str, cls: type[BaseModel]) -> Path:
        image.parent.mkdir(parents=True)
        image.write_text(content)
        return original(plugin, cls)

    with pytest.raises(expected_error):
        bind_plugin_config("invalid-winner", BootConfig, configs, write_default_fn=competing_write)
    with pytest.raises(KeyError):
        get_plugin_config(
            "invalid-winner",
            AgentSlices.resolve(
                plugin_configs=configs,
                default_reader=defaults(),
            ),
            BootConfig,
        )
    assert image.read_text() == content


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_bind_propagates_unknown_creation_errors(
    unit_home: Path, error_type: type[Exception]
) -> None:
    configs: dict[str, BaseModel] = {}
    error = error_type("unexpected create failure")

    def fail_write(plugin: str, cls: type[BaseModel]) -> Path:
        raise error

    with pytest.raises(error_type) as captured:
        bind_plugin_config("unknown-writer-error", BootConfig, configs, write_default_fn=fail_write)
    assert captured.value is error
    assert not disk_image_path("unknown-writer-error").exists()
    with pytest.raises(KeyError):
        get_plugin_config(
            "unknown-writer-error",
            AgentSlices.resolve(
                plugin_configs=configs,
                default_reader=defaults(),
            ),
            BootConfig,
        )


def test_default_writer_still_rejects_an_existing_image(unit_home: Path) -> None:
    image = disk_image_path("create-only")
    write_config_image(
        PluginConfigOwner("create-only", BootConfig, image),
        BootConfig(marker="existing"),
        expected_digest=image_revision(None),
    )
    before = image.read_bytes()
    with pytest.raises(PluginConfigChangedError):
        config_registration.write_default_disk_image("create-only", BootConfig)
    assert image.read_bytes() == before


def test_bind_does_not_replace_a_disappearing_winner_with_defaults(
    unit_home: Path,
) -> None:
    configs: dict[str, BaseModel] = {}
    image = disk_image_path("missing-winner")

    def disappearing_write(plugin: str, cls: type[BaseModel]) -> Path:
        image.parent.mkdir(parents=True)
        image.write_text(BootConfig(marker="winner").model_dump_json())
        image.unlink()
        raise PluginConfigChangedError("winning image disappeared")

    with pytest.raises(FileNotFoundError):
        bind_plugin_config(
            "missing-winner", BootConfig, configs, write_default_fn=disappearing_write
        )
    assert not image.exists()
    with pytest.raises(KeyError):
        get_plugin_config(
            "missing-winner",
            AgentSlices.resolve(
                plugin_configs=configs,
                default_reader=defaults(),
            ),
            BootConfig,
        )


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_bind_propagates_unknown_winning_image_errors(
    unit_home: Path, error_type: type[Exception]
) -> None:
    configs: dict[str, BaseModel] = {}
    parser = config_registration.config_from_image
    error = error_type("unexpected winning image failure")
    image = disk_image_path("unknown-read-error")

    def fail_read(cls: type[BaseModel], content: str, path: Path) -> BaseModel:
        if path == image:
            raise error
        return parser(cls, content, path)

    with pytest.raises(error_type) as captured:
        bind_plugin_config(
            "unknown-read-error", BootConfig, configs, config_from_image_fn=fail_read
        )
    assert captured.value is error
    assert json.loads(image.read_text()) == {"marker": "default"}
    with pytest.raises(KeyError):
        get_plugin_config(
            "unknown-read-error",
            AgentSlices.resolve(
                plugin_configs=configs,
                default_reader=defaults(),
            ),
            BootConfig,
        )


if __name__ == "__main__":
    _run_boot_race_child(sys.argv[1])
