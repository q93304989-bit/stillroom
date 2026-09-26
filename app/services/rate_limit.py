"""按能力的滑动窗口限流。

平台硬限制写在能力元数据里（`ToolSpec.rate_limit`），这里负责执行。视频接口
「每分钟只允许提交 1 个任务」，旧版靠界面提示 + 失败后退避 60 秒绕开，现在由调度
统一排队——将来工作流批量跑的时候这步是必需的，否则会被服务端连续打回。
"""

from __future__ import annotations

from typing import Mapping

from app.services.clock import Clock, SystemClock

WINDOW_SECONDS = 60.0


class RateLimiter:
    """按 key（通常是能力名）限流。进程内实现：单机单进程够用。"""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._limits: dict[str, int] = {}
        self._window: dict[str, float] = {}
        self._stamps: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- 配置

    def configure(
        self, key: str, per_minute: int, *, window: float = WINDOW_SECONDS
    ) -> "RateLimiter":
        """给某个能力设限额（返回自身，便于链式）；`per_minute <= 0` 表示不限制。"""
        if per_minute <= 0:
            self._limits.pop(key, None)
            return self
        self._limits[key] = int(per_minute)
        self._window[key] = float(window)
        self._stamps.setdefault(key, [])
        return self

    def configure_from(self, limits: Mapping[str, Mapping[str, int]]) -> "RateLimiter":
        """直接从能力注册表的 `rate_limits()` 配置（元数据驱动，不再各处硬编码）。"""
        for key, spec_limit in (limits or {}).items():
            per_minute = int(spec_limit.get("per_minute", 0) or 0)
            if per_minute:
                self.configure(key, per_minute)
        return self

    def limit_of(self, key: str) -> int:
        return self._limits.get(key, 0)

    # ---------------------------------------------------------------- 放行

    def retry_after(self, key: str) -> float:
        """还要等多久才可能放行；0 表示现在就可以。"""
        limit = self._limits.get(key, 0)
        if limit <= 0:
            return 0.0
        window = self._window.get(key, WINDOW_SECONDS)
        now = self._clock.now()
        stamps = [t for t in self._stamps.get(key, []) if now - t < window]
        self._stamps[key] = stamps
        if len(stamps) < limit:
            return 0.0
        return max(0.0, window - (now - min(stamps)))

    async def acquire(self, key: str) -> float:
        """排队直到可以调用；返回实际等待的秒数（会写进事件里）。"""
        waited = 0.0
        while True:
            delay = self.retry_after(key)
            if delay <= 0:
                self._stamps.setdefault(key, []).append(self._clock.now())
                return waited
            await self._clock.sleep(delay)
            waited += delay

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._stamps.clear()
        else:
            self._stamps.pop(key, None)
