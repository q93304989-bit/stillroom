"""单一状态源。

旧版把生成状态、历史选中、动画代数等 221 个字段全挂在 god class 上，界面只能自己
拼凑「现在是什么状态」。这里改成分域快照 + 订阅通知：

    state.get("generation")        # 读取当前生成状态
    state.subscribe(fn)            # 状态变化时收到 (section, snapshot)
    state.track(handle)            # 接上任务句柄，自动跟随事件更新
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Mapping

from app.state.events import Event, EventBus
from app.state.jobs import JobHandle

#: 状态分域（界面按域订阅，而不是订阅整份状态）
SECTIONS = ("generation", "history", "runs")


def _empty_section(name: str) -> dict[str, Any]:
    if name == "generation":
        return {
            "job_id": "",
            "tool": "",
            "status": "idle",
            "progress": None,
            "message": "",
            "result": None,
            "error": "",
            "error_kind": "",
        }
    if name == "history":
        return {"selected_id": "", "filter": "all", "keyword": "", "count": 0}
    return {"active_run": "", "steps": 0}      # runs：为未来的工作流预留


class AppState:
    """分域状态 + 订阅通知。所有写入都要经过 `set()`，订阅者才一定收得到。"""

    def __init__(self, bus: EventBus | None = None) -> None:
        self._sections: dict[str, dict[str, Any]] = {name: _empty_section(name) for name in SECTIONS}
        self._subscribers: list[tuple[weakref.ref | None, Callable[[str, dict], None]]] = []
        self._unsubscribes: list[Callable[[], None]] = []
        self.bus = bus or EventBus()

    # ---------------------------------------------------------------- 读写

    def get(self, section: str, key: str | None = None) -> Any:
        data = self._sections.setdefault(section, {})
        return data if key is None else data.get(key)

    def snapshot(self, section: str | None = None) -> dict[str, Any]:
        if section:
            return dict(self._sections.get(section, {}))
        return {name: dict(data) for name, data in self._sections.items()}

    def set(self, section: str, **fields: Any) -> dict[str, Any]:
        """写入若干字段并通知订阅者（值没变则安静，避免无意义刷新）。"""
        data = self._sections.setdefault(section, {})
        changed = {key: value for key, value in fields.items() if data.get(key) != value}
        if not changed:
            return data
        data.update(changed)
        self._notify(section)
        return data

    # ---------------------------------------------------------------- 订阅

    def subscribe(self, callback: Callable[[str, dict], None]) -> Callable[[], None]:
        ref = weakref.WeakMethod(callback) if hasattr(callback, "__self__") else None
        self._subscribers.append((ref, callback))

        def _unsubscribe() -> None:
            self._subscribers = [
                (r, fn) for (r, fn) in self._subscribers if fn is not callback
            ]

        return _unsubscribe

    def _notify(self, section: str) -> None:
        payload = dict(self._sections.get(section, {}))
        for ref, callback in list(self._subscribers):
            if ref is not None and ref() is None:
                continue
            try:
                callback(section, payload)
            except Exception as exc:      # 单个订阅者出错不影响其他人
                print(f"[state] 订阅者处理 {section} 出错: {exc}")

    # ---------------------------------------------------------------- 任务接入

    def track(self, handle: JobHandle) -> JobHandle:
        """把任务句柄接进状态：事件会自动折算成 generation 分域的字段。

        这是「界面只订阅、不轮询」的落点——界面无需知道任务是怎么跑的。
        """
        self.set(
            "generation",
            job_id=handle.id,
            tool=handle.job.tool,
            status=handle.status.value,
            progress=None,
            message="",
            result=None,
            error="",
            error_kind="",
        )

        def _on_event(event: Event) -> None:
            if event.job_id != handle.id:
                return
            mapping: dict[str, Mapping[str, Any]] = {
                "job.started": {"status": "running"},
                "job.progress": {
                    "status": "running",
                    "progress": event.get("progress"),
                    "message": event.get("message", ""),
                },
                "job.retrying": {"status": "running", "message": event.get("message", "")},
                "job.succeeded": {
                    "status": "succeeded",
                    "progress": 100,
                    "result": event.get("result"),
                    "message": event.get("message", ""),
                },
                "job.failed": {
                    "status": "failed",
                    "error": event.get("error", ""),
                    "error_kind": event.get("error_kind", ""),
                    "message": event.get("message", ""),
                },
                "job.canceled": {"status": "canceled", "message": event.get("message", "已取消")},
            }
            fields = mapping.get(event.type)
            if fields:
                self.set("generation", **fields)

        self._unsubscribes.append(self.bus.subscribe(_on_event))
        return handle

    def clear_tracking(self) -> None:
        """断开所有由 `track()` 建立的订阅（界面重建时用）。"""
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes.clear()
