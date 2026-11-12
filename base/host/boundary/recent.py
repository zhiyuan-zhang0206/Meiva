from __future__ import annotations

from collections.abc import Iterator


# meiva:note
# The two containers a long-lived component keeps what it remembers of a long conversation in: insertion-ordered, with a fixed upper
# bound, so that what it remembers never grows with the length of the conversation. When a container is full the
# oldest entry is forgotten first; re-adding or re-setting a present item keeps its place in the order.
class RecentSet[T]:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: dict[T, None] = {}

    def add(self, item: T) -> None:
        if item in self._items:
            return
        self._items[item] = None
        if len(self._items) > self._limit:
            del self._items[next(iter(self._items))]

    def discard(self, item: T) -> None:
        _ = self._items.pop(item, None)

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)


class RecentMap[K, V]:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: dict[K, V] = {}

    def __setitem__(self, key: K, value: V) -> None:
        self._items[key] = value
        if len(self._items) > self._limit:
            del self._items[next(iter(self._items))]

    def __getitem__(self, key: K) -> V:
        return self._items[key]

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[K]:
        return iter(self._items)

    def get(self, key: K) -> V | None:
        return self._items.get(key)

    def pop(self, key: K) -> V | None:
        return self._items.pop(key, None)

    def values(self) -> Iterator[V]:
        return iter(self._items.values())

    def items(self) -> Iterator[tuple[K, V]]:
        return iter(self._items.items())
