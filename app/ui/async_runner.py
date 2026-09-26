"""asyncio 事件循环的宿主线程。

为什么不用 qasync：那是一个额外依赖，而这里需要的东西很少——**把协程提交到另一个
线程的循环里执行，结果用 Qt 信号回来**。自己写四十行比引入依赖更可控，也不影响
用户那边本就缓慢的网络。

线程模型：

    Qt 主线程                      asyncio 线程
    ├─ QML 渲染 / 信号槽           ├─ httpx 请求
    ├─ bridge 槽函数 ──submit────→ ├─ 生成编排（限流、轮询、重试）
    └─ 信号回调 ←────emit──────── ┘

Qt 信号跨线程 emit 是线程安全的（接收方在哪个线程就排队到哪个线程），因此 bridge 可以
放心在 asyncio 线程里 emit。
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Callable, Coroutine


class AsyncRunner:
    """专用 asyncio 线程。`submit()` 提交协程，`call()` 在循环线程里执行普通函数。"""

    def __init__(self, name: str = "agnes-asyncio") -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    # ---------------------------------------------------------------- 生命周期

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        # run_forever 退出后收尾：取消残留任务，避免解释器退出时报 never awaited
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self._loop.close()

    def close(self, timeout: float = 3.0) -> bool:
        """停掉循环线程，返回「确认线程已退出」。

        为什么非要确认：线程没死就等于**收尾没收干净**——它还会继续跑任务、继续 emit
        信号，而调用方（界面退出、测试换用例）已经开始拆对象、建新引擎了。实测过一次
        偶发崩溃就发生在「旧循环线程还在轮询、主线程在建新的 QML 引擎」的时刻，
        所以这里把「停不掉」这件事变成明确的返回值，由调用方决定怎么办。
        """
        if not self._thread.is_alive():
            return True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            # 收尾里的 gather 可能在等一个迟迟不返回的调用（比如 to_thread 里的落库），
            # 再给一次机会；两次都没停掉就如实返回 False。
            self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    # ---------------------------------------------------------------- 提交

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        """把协程丢进循环线程执行，返回 concurrent.futures.Future。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call(self, fn: Callable[..., Any], *args: Any) -> None:
        """在循环线程里执行一个普通函数（例如 `task.cancel()`）。"""
        self._loop.call_soon_threadsafe(fn, *args)

    def run_blocking(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        """提交并等待结果（只在测试或退出收尾时用，别在界面线程里等）。"""
        return self.submit(coro).result(timeout)
