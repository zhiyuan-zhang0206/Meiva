from typing import Protocol


class AskUser(Protocol):
    def __call__(self, questions: list[dict[str, object]]) -> list[int]: ...
