"""Tiny in-process TTL cache for GET responses. Cleared after every analysis run.

Enough for a single backend process; with several workers use Redis (Phase 3).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .config import settings

_store: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
stats = {"hits": 0, "misses": 0}


def get_or_set(key: str, fn: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit and hit[0] > now:
            stats["hits"] += 1
            return hit[1]
    value = fn()
    with _lock:
        stats["misses"] += 1
        if settings.api_cache_ttl > 0:
            _store[key] = (now + settings.api_cache_ttl, value)
    return value


def clear() -> None:
    with _lock:
        _store.clear()
