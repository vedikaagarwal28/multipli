"""
Minimal in-memory TTL cache. Not shared across processes and not
persisted — that's fine for a single-instance hackathon deploy. If
this needs to survive restarts or run behind multiple workers later,
swap this for Redis without touching call sites (same get/set shape).
"""
import time
from typing import Any, Optional


class TTLCache:
    def __init__(self, ttl_seconds: int = 600):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        hit = self._store.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl
        self._store[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._store.clear()
