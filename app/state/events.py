"""结构化事件总线。

事件类型（约定）：

    job.created / job.started / job.progress / job.retrying
    job.succeeded / job.failed / job.canceled
    tool.called / tool.returned
    artifact.created

设计取舍：**同步扇出**（单个订阅者异常不影响其他订阅者），只在内存里保留最近 N 条。
落库与回放属于未来的工作流阶段，本次不做——事件流的作用是让界面不必轮询。
"""

from __future__ import annotations

import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

MAX_RECENT_EVENTS = 500


@dataclass(frozen=True)
class Event:
    """一条结构化事件。`payload` 放该类型需要的字段（进度、耗时、错误分类等）。"""

    type: str
    job_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


class EventBus:
    """最小的发布 / 订阅实现。"""

    def __init__(self, keep: int = MAX_RECENT_EVENTS) -> None:
        self._subscribers: list[tuple[weakref.ref | None, Callable[[Event], None]]] = []
        self._recent: deque[Event] = deque(maxlen=keep)

    # ---------------------------------------------------------------- 订阅

    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """订阅事件；返回取消订阅的函数。

        绑定方法用弱引用保存，目标销毁后自动失效（避免界面重建后残留订阅）。
        """
        ref = weakref.WeakMethod(callback) if hasattr(callback, "__self__") else None
        self._subscribers.append((ref, callback))
        return lambda: self.unsubscribe(callback)

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        self._subscribers = [
            (ref, fn)
            for ref, fn in self._subscribers
            if fn is not callback and (ref is None or ref() is not None)
        ]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ---------------------------------------------------------------- 广播

    def emit(self, event: Event) -> Event:
        self._recent.append(event)
        for ref, callback in list(self._subscribers):
            if ref is not None and ref() is None:      # 目标已销毁
                continue
            try:
                callback(event)
            except Exception as exc:                  # 单个订阅者出错不影响其他订阅者
                print(f"[events] 订阅者处理 {event.type} 出错: {exc}")
        return event

    def emit_all(self, events: Iterable[Event]) -> None:
        for event in events:
            self.emit(event)

    def recent(self, *, job_id: str = "", limit: int = 50) -> list[Event]:
        """最近的事件（调试与测试用；正式回放留到工作流阶段）。"""
        items = [e for e in self._recent if not job_id or e.job_id == job_id]
        return items[-limit:]

    def clear(self) -> None:
        self._recent.clear()
