"""Exact-match response cache (in-memory LRU, or Redis when configured)."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from gateway.schemas import ChatRequest


def cache_key(route: str, req: ChatRequest) -> str:
    payload = {
        "route": route,
        "system": req.system,
        "messages": [m.model_dump() for m in req.messages],
        "max_tokens": req.max_tokens,
        "effort": req.effort,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"llmgw:{digest}"


class Cache(Protocol):
    async def get(self, key: str) -> dict | None: ...
    async def set(self, key: str, value: dict) -> None: ...


class MemoryCache:
    def __init__(self, ttl_seconds: int, max_entries: int, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._items: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    async def get(self, key: str) -> dict | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self.clock() >= expires_at:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    async def set(self, key: str, value: dict) -> None:
        self._items[key] = (self.clock() + self.ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)


class RedisCache:
    def __init__(self, url: str, ttl_seconds: int):
        import redis.asyncio as redis  # optional dependency: pip install .[redis]

        self.client = redis.from_url(url)
        self.ttl = ttl_seconds

    async def get(self, key: str) -> dict | None:
        raw = await self.client.get(key)
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: dict) -> None:
        await self.client.set(key, json.dumps(value), ex=self.ttl)
