from __future__ import annotations

from collections import OrderedDict

from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse

__all__ = ["CACHE_ENTRIES", "SuggestionCache"]

CACHE_ENTRIES = 512


# meiva:note
# The newest suggestions per (agent, last agent message), least recently used out first.
class SuggestionCache:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[tuple[int, str], ReplySuggestionResponse] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: tuple[int, str]) -> ReplySuggestionResponse | None:
        found = self._entries.get(key)
        if found is not None:
            self._entries.move_to_end(key)
        return found

    def remember(self, key: tuple[int, str], result: ReplySuggestionResponse) -> None:
        self._entries[key] = result
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
