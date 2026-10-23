# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
"""Whole plugin config images: validation, revision and one locked atomic writer."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from base.host.atomic_io import write_bytes_atomic
from base.host.env.dotenv_file import ENV_LOCK_TIMEOUT_S
from base.native_process.os_platform import file_lock
from base.packages.plugin_config_contracts import ConfigImage
from base.packages.plugin_config_contracts import (
    PluginConfigChangedError as PluginConfigChangedError,
)


def config_image_path(plugin: str, *, home: Path) -> Path:
    # meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
    return home.resolve() / "configs" / plugin / "config.json"


@dataclass(frozen=True)
class PluginConfigOwner:
    """One pure declaration and its existing authority image."""

    name: str
    cls: type[BaseModel]
    path: Path

    def read(self) -> str:
        return self.path.read_text()

    def create_default(self) -> None:
        write_config_image(self, self.cls(), expected_digest=image_revision(None))


@dataclass(frozen=True)
class DiskPluginConfigImages:
    # meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md

    home: Path

    def owner(self, name: str, cls: type[BaseModel]) -> PluginConfigOwner:
        return PluginConfigOwner(name, cls, config_image_path(name, home=self.home))


def image_digest(path: Path) -> str:
    """Digest the complete authority bytes; an absent image has empty bytes."""
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        payload = None
    return image_revision(payload)


def image_revision(payload: bytes | None) -> str:
    """Distinguish a missing whole image from an existing invalid empty file."""
    return hashlib.sha256(b"" if payload is None else b"\x01" + payload).hexdigest()


def write_config_image(
    owner: ConfigImage,
    config: BaseModel,
    *,
    expected_digest: str,
) -> None:
    """CAS one complete validated image under its existing cross-process file lock."""
    values = config.model_dump()
    if set(values) != set(owner.cls.model_fields):
        raise ValueError("plugin config write requires the complete declared image")
    validated = owner.cls.model_validate(values)
    owner.path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(owner.path.with_suffix(".lock"), timeout_s=ENV_LOCK_TIMEOUT_S):
        if image_digest(owner.path) != expected_digest:
            raise PluginConfigChangedError("plugin config changed before owned image write")
        write_bytes_atomic(
            owner.path,
            (
                json.dumps(validated.model_dump(mode="json"), indent=2, allow_nan=False) + "\n"
            ).encode(),
            mode=0o600,
        )
