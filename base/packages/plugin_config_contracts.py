"""Required collaborators for reading and initializing complete plugin images."""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class PluginConfigChangedError(RuntimeError):
    """The captured authority image changed before its owned write."""


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
