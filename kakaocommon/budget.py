"""A least-recently-used cache bounded by the bytes it holds, not its entries.

A count of entries says nothing about memory when one entry is a few hundred
bytes and the next is eight megabytes of photo. This cache counts the size of
what it keeps and drops the oldest entries once the total passes its limit.

Some entries must stay however old they are: a picture on screen is drawn
from its bytes on every frame, and evicting it would mean fetching and
converting it again on the next one. ``put`` takes the keys to keep, and
never evicts the key it was just given, so the limit can be exceeded by what
is in use but never by what is merely remembered.
"""

from __future__ import annotations

from typing import Callable, Dict, Generic, Iterable, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class BudgetedCache(Generic[K, V]):
    def __init__(
        self,
        limit_bytes: int,
        *,
        max_entries: Optional[int] = None,
        size: Callable[[V], int] = len,  # type: ignore[assignment]
    ) -> None:
        self.limit_bytes = limit_bytes
        self.max_entries = max_entries
        self._size = size
        self._items: Dict[K, V] = {}
        self._sizes: Dict[K, int] = {}
        self.total = 0
        """Bytes held right now, by the size function's count."""

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, key: K) -> V:
        value = self._items.pop(key)
        self._items[key] = value
        return value

    def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        if key not in self._items:
            return default
        return self[key]

    def put(self, key: K, value: V, keep: Iterable[K] = ()) -> None:
        """Remember *value*, then shed the oldest entries not in *keep*."""
        self.pop(key)
        self._items[key] = value
        size = self._size(value)
        self._sizes[key] = size
        self.total += size
        kept = set(keep)
        kept.add(key)
        for old in list(self._items):
            over_bytes = self.total > self.limit_bytes
            over_count = self.max_entries is not None and len(self._items) > self.max_entries
            if not (over_bytes or over_count):
                break
            if old not in kept:
                self.pop(old)

    def pop(self, key: K, default: Optional[V] = None) -> Optional[V]:
        if key not in self._items:
            return default
        self.total -= self._sizes.pop(key)
        return self._items.pop(key)

    def clear(self) -> None:
        self._items.clear()
        self._sizes.clear()
        self.total = 0
