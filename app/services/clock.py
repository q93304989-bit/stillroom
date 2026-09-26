"""时间与睡眠的注入点。

存在的唯一理由：让「退避 10 秒」「每分钟只允许 1 个任务」这类策略**可被测试**。
测试里换成虚拟时钟，几秒的策略瞬间跑完，而断言的是真实等待时长。
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """线上实现：单调时钟 + asyncio.sleep。"""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class VirtualClock:
    """虚拟时钟（测试用）：sleep 直接推进时间，不真的等待。"""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)
        self.slept: list[float] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.slept.append(seconds)
            self._now += seconds
        await asyncio.sleep(0)      # 让出控制权，保证任务仍可被取消

    def advance(self, seconds: float) -> None:
        self._now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)
