"""Tests for utils.async_cache, the shared LRU decorator for async functions."""

from __future__ import annotations

import pytest

from utils.async_cache import async_lru_cache


async def test_cache_hit_returns_cached_result():
    call_count = 0

    @async_lru_cache(maxsize=10)
    async def fetch(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    assert await fetch(3) == 6
    assert call_count == 1
    # Second call with same arg should hit cache.
    assert await fetch(3) == 6
    assert call_count == 1
    # Different arg recomputes.
    assert await fetch(4) == 8
    assert call_count == 2


async def test_last_was_hit_attribute_tracks_hits():
    @async_lru_cache(maxsize=10)
    async def fetch(x: int) -> int:
        return x

    await fetch(1)
    assert fetch._last_was_hit is False  # type: ignore[attr-defined]
    await fetch(1)
    assert fetch._last_was_hit is True  # type: ignore[attr-defined]
    await fetch(2)
    assert fetch._last_was_hit is False  # type: ignore[attr-defined]


async def test_kwargs_in_cache_key():
    call_count = 0

    @async_lru_cache(maxsize=10)
    async def fetch(x: int, *, multiplier: int = 1) -> int:
        nonlocal call_count
        call_count += 1
        return x * multiplier

    await fetch(3, multiplier=2)
    await fetch(3, multiplier=2)  # cache hit
    await fetch(3, multiplier=3)  # different kwarg, recomputes
    assert call_count == 2


async def test_maxsize_evicts_oldest():
    @async_lru_cache(maxsize=2)
    async def fetch(x: int) -> int:
        return x

    await fetch(1)
    await fetch(2)
    await fetch(3)  # evicts (1,)
    # (2,) and (3,) should still be cached; (1,) recomputes.
    assert fetch._last_was_hit is False  # type: ignore[attr-defined]
    await fetch(2)
    assert fetch._last_was_hit is True  # type: ignore[attr-defined]
    await fetch(1)
    assert fetch._last_was_hit is False  # type: ignore[attr-defined]


async def test_lru_access_promotes_entry():
    @async_lru_cache(maxsize=2)
    async def fetch(x: int) -> int:
        return x

    await fetch(1)
    await fetch(2)
    # Re-access (1,) so it becomes the most-recently used; the next miss should
    # evict (2,), not (1,).
    await fetch(1)
    await fetch(3)
    await fetch(1)
    assert fetch._last_was_hit is True  # type: ignore[attr-defined]
    await fetch(2)
    assert fetch._last_was_hit is False  # type: ignore[attr-defined]


async def test_cache_exposes_underlying_dict_for_inspection():
    @async_lru_cache(maxsize=5)
    async def fetch(x: int) -> int:
        return x * 10

    await fetch(1)
    await fetch(2)
    assert len(fetch._cache) == 2  # type: ignore[attr-defined]


async def test_async_lru_cache_does_not_share_state_between_decorators():
    """Each decorated function gets its own cache."""

    @async_lru_cache(maxsize=10)
    async def fa(x: int) -> int:
        return x

    @async_lru_cache(maxsize=10)
    async def fb(x: int) -> int:
        return x

    await fa(1)
    assert len(fa._cache) == 1  # type: ignore[attr-defined]
    assert len(fb._cache) == 0  # type: ignore[attr-defined]


async def test_preserves_wrapped_function_metadata():
    @async_lru_cache(maxsize=10)
    async def my_named_func(x: int) -> int:
        """Docstring."""
        return x

    assert my_named_func.__name__ == "my_named_func"
    assert my_named_func.__doc__ == "Docstring."


async def test_propagates_exceptions_without_caching():
    call_count = 0

    @async_lru_cache(maxsize=10)
    async def flaky(x: int) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first call fails")
        return x

    with pytest.raises(RuntimeError):
        await flaky(1)
    # Second call should re-run (exception wasn't cached) and succeed.
    assert await flaky(1) == 1
    assert call_count == 2
