import os
import sys
from pathlib import Path

from base.host import private_storage


class _CrashPath(Path):
    stage: str = ""
    linked: bool = False

    def with_name(self, name: str) -> Path:
        return Path(super().__fspath__()).with_name(name)

    def __fspath__(self) -> str:
        if self.stage == "before-link":
            os._exit(73)
        self.linked = True
        return super().__fspath__()

    @property
    def parent(self) -> Path:
        if self.linked:
            os._exit(73)
        return Path(super().__fspath__()).parent


if __name__ == "__main__":
    target = _CrashPath(sys.argv[1])
    target.stage = sys.argv[2]
    private_storage.create_private_bytes(target, b"bytes")
