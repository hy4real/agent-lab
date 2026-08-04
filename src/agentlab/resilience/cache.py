"""LRU + TTL cache with deterministic clock injection."""

from __future__ import annotations

from datetime import datetime
from typing import Callable


class TTLCache:
    def __init__(
        self,
        *,
        capacity: int,
        clock: Callable[[], str],
        ttl_seconds: int | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须为正整数")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须为正整数或 None")
        self.capacity = capacity
        self.clock = clock
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[str, str]] = {}
        self._order: list[str] = []
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> str | None:
        item = self._store.get(key)
        if item is None:
            self._misses += 1
            return None
        value, stored_at = item
        if self._is_expired(key, stored_at):
            self._remove(key)
            self._misses += 1
            return None
        self._touch(key)
        self._hits += 1
        return value

    def put(self, key: str, value: str) -> None:
        if key in self._store:
            self._remove(key)
        self._store[key] = (value, self.clock())
        self._order.append(key)
        while len(self._store) > self.capacity:
            self._evict_lru()

    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "evictions": self._evictions, "size": len(self._store)}

    def _is_expired(self, key: str, stored_at: str) -> bool:
        if self.ttl_seconds is None:
            return False
        now = datetime.fromisoformat(self.clock().replace("Z", "+00:00"))
        stored = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
        return (now - stored).total_seconds() >= self.ttl_seconds

    def _touch(self, key: str) -> None:
        self._order.remove(key)
        self._order.append(key)

    def _remove(self, key: str) -> None:
        del self._store[key]
        self._order.remove(key)

    def _evict_lru(self) -> None:
        key = self._order.pop(0)
        del self._store[key]
        self._evictions += 1
