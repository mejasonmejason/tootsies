"""Async-friendly LRU cache decorator.

functools.lru_cache doesn't work with coroutine functions, so this module
provides an equivalent decorator that does. Each call's arg tuple is hashed
the same way functools does, and on a cache hit the cached awaited result is
returned without re-running the coroutine.

The decorator exposes two attributes on the wrapped function for callers that
care about cache observability:

- `_last_was_hit`: True if the most recent call returned a cached result.
  Used by callers that emit per-call telemetry events (e.g. utils/markets.py
  records `cache_hit` on the `market_fetch` event so dashboards can track
  hit rate). Naturally racy under concurrency; treat it as best-effort.
- `_cache`: the underlying OrderedDict, exposed for tests + inspection.

Used by utils/link_enrich.py and utils/markets.py. If a third caller adds a
new use, the same decorator works without changes.
"""

from __future__ import annotations

import functools
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

_DEFAULT_MAXSIZE = 256


def async_lru_cache(
    maxsize: int = _DEFAULT_MAXSIZE,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """LRU-cache decorator for async functions.

    Args:
        maxsize: maximum cached entries; oldest are evicted FIFO. Default 256.

    Returns:
        A decorator that wraps an async function and caches its results.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        cache: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        sentinel = object()

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = (args, tuple(sorted(kwargs.items())))
            cached = cache.get(key, sentinel)
            if cached is not sentinel:
                cache.move_to_end(key)
                wrapper._last_was_hit = True  # type: ignore[attr-defined]
                return cached
            wrapper._last_was_hit = False  # type: ignore[attr-defined]
            result = await func(*args, **kwargs)
            cache[key] = result
            if len(cache) > maxsize:
                cache.popitem(last=False)
            return result

        wrapper._last_was_hit = False  # type: ignore[attr-defined]
        wrapper._cache = cache  # type: ignore[attr-defined]
        return wrapper

    return decorator
