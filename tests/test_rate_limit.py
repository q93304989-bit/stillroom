"""限流器：滑动窗口行为（用虚拟时钟，断言真实等待时长）。"""

from __future__ import annotations

from app.services.clock import VirtualClock
from app.services.rate_limit import RateLimiter


def test_unlimited_key_passes_immediately():
    clock = VirtualClock()
    limiter = RateLimiter(clock)
    return_value = None

    async def run():
        nonlocal return_value
        return_value = await limiter.acquire("free")

    import asyncio

    asyncio.run(run())
    assert return_value == 0.0
    assert clock.total_slept == 0


async def test_first_call_is_free_second_waits_full_window():
    """视频提交：每分钟 1 个。第二次提交必须等到窗口过去。"""
    clock = VirtualClock()
    limiter = RateLimiter(clock).configure("video.submit", 1)

    assert await limiter.acquire("video.submit") == 0.0
    assert limiter.retry_after("video.submit") == 60.0
    assert await limiter.acquire("video.submit") == 60.0
    assert clock.total_slept == 60.0


async def test_window_expiry_frees_the_slot():
    clock = VirtualClock()
    limiter = RateLimiter(clock).configure("video.submit", 1)

    await limiter.acquire("video.submit")
    clock.advance(60.1)

    assert limiter.retry_after("video.submit") == 0.0
    assert await limiter.acquire("video.submit") == 0.0
    assert clock.total_slept == 0


async def test_configure_from_registry_metadata():
    """限额来自能力元数据，不是各处硬编码。"""
    clock = VirtualClock()
    limiter = RateLimiter(clock).configure_from(
        {"video.submit": {"per_minute": 1}, "image.generate": {"per_minute": 10}}
    )

    assert limiter.limit_of("video.submit") == 1
    assert limiter.limit_of("image.generate") == 10
    assert limiter.limit_of("media.fetch") == 0


async def test_higher_limit_allows_burst_then_throttles():
    clock = VirtualClock()
    limiter = RateLimiter(clock).configure("image.generate", 2)

    assert await limiter.acquire("image.generate") == 0.0
    assert await limiter.acquire("image.generate") == 0.0
    assert await limiter.acquire("image.generate") == 60.0


async def test_reset_clears_history():
    clock = VirtualClock()
    limiter = RateLimiter(clock).configure("video.submit", 1)
    await limiter.acquire("video.submit")
    limiter.reset("video.submit")
    assert await limiter.acquire("video.submit") == 0.0
