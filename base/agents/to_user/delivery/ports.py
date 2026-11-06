from typing import Protocol


class AskUser(Protocol):
    def __call__(self, *, title: str, content: str) -> int: ...
