# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class PluginConfigChangedError(RuntimeError):
    # meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
    pass


class ConfigImage(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def cls(self) -> type[BaseModel]: ...

    @property
    def path(self) -> Path: ...

    def read(self) -> str: ...

    def create_default(self) -> None: ...


class PluginConfigImages(Protocol):
    def owner(self, name: str, cls: type[BaseModel]) -> ConfigImage: ...


class UnboundPluginConfigImages:
    # meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md

    def owner(self, name: str, cls: type[BaseModel]) -> ConfigImage:
        raise RuntimeError(
            f"SDK surface carries no config-image owner for plugin {name!r} ({cls.__name__})"
        )
