#!/usr/bin/env python3
"""
data/cache.py
─────────────────────────────────────────────────────────────────────────────
Simple thread-safe TTL cache for sport data fetchers.
Each fetcher uses this to avoid redundant API calls within a cycle.
"""

import threading
import time
from typing import Any, Optional


class TTLCache:
    """
    Thread-safe key-value cache with per-entry TTL.
    
    Usage:
        cache = TTLCache(default_ttl=30)
        cache.set("nba_games", data, ttl=60)
        data = cache.get("nba_games")  # None if expired
    """

    def __init__(self, default_ttl: float = 30.0):
        self._default_ttl = default_ttl
        self._store: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        ttl = ttl if ttl is not None else self._default_ttl
        with self._lock:
            self._store[key] = (value, time.time() + ttl)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None


# Shared cache instances per data domain
espn_cache   = TTLCache(default_ttl=45)   # ESPN game data — one cycle
tennis_cache = TTLCache(default_ttl=20)   # Tennis live scores — sub-cycle
nba_cache    = TTLCache(default_ttl=45)
mlb_cache    = TTLCache(default_ttl=45)
market_cache = TTLCache(default_ttl=10)   # Market prices — short TTL
