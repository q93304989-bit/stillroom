"""任务句柄、事件总线、状态源：契约层的三件套。"""

from __future__ import annotations

import asyncio

import pytest

from app.state.app_state import AppState
from app.state.events import Event, EventBus
from app.state.jobs import Job, JobHandle, JobStatus


# --------------------------------------------------------------------------- 事件总线

async def test_event_bus_fanout_and_unsubscribe():
    bus = EventBus()
    seen: list[str] = []
    unsubscribe = bus.subscribe(lambda event: seen.append(event.type))

    bus.emit(Event(type="a"))
    bus.emit(Event(type="b"))
    unsubscribe()
    bus.emit(Event(type="c"))

    assert seen == ["a", "b"]
    assert [e.type for e in bus.recent()] == ["a", "b", "c"]


async def test_event_bus_isolates_subscriber_errors():
    bus = EventBus()
    seen: list[str] = []

    def bad(_event: Event) -> None:
        raise RuntimeError("订阅者自己炸了")

    bus.subscribe(bad)
    bus.subscribe(lambda event: seen.append(event.type))
    bus.emit(Event(type="x"))

    assert seen == ["x"]          # 一个订阅者出错不影响其他订阅者


async def test_event_recent_filters_by_job():
    bus = EventBus()
    bus.emit(Event(type="a", job_id="j1"))
    bus.emit(Event(type="b", job_id="j2"))
    assert [e.type for e in bus.recent(job_id="j1")] == ["a"]


async def test_event_bus_drops_dead_bound_methods():
    class Listener:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def on_event(self, event: Event) -> None:
            self.seen.append(event.type)

    bus = EventBus()
    listener = Listener()
    bus.subscribe(listener.on_event)
    bus.emit(Event(type="a"))
    assert listener.seen == ["a"]

    del listener                    # 目标销毁：弱引用失效
    bus.emit(Event(type="b"))       # 不应抛异常
    assert bus.subscriber_count == 1


# --------------------------------------------------------------------------- 任务句柄

async def test_job_handle_wait_returns_job():
    job = Job(tool="t")

    async def runner() -> None:
        job.status = JobStatus.SUCCEEDED
        job.result = {"ok": True}

    handle = JobHandle(job, asyncio.create_task(runner()))
    done = await handle.wait()

    assert done.status is JobStatus.SUCCEEDED
    assert handle.result == {"ok": True}
    assert handle.done


async def test_job_handle_cancel_marks_canceled():
    job = Job(tool="t")

    async def runner() -> None:
        await asyncio.sleep(3600)

    handle = JobHandle(job, asyncio.create_task(runner()))
    await asyncio.sleep(0)
    handle.cancel()
    done = await handle.wait()

    assert done.status is JobStatus.CANCELED
    assert done.finished_at is not None


def test_job_to_dict_shape():
    job = Job(tool="image.generate", params={"prompt": "x"}, parent_job_id="run-1")
    payload = job.to_dict()
    assert payload["tool"] == "image.generate"
    assert payload["status"] == "pending"
    assert payload["parent_job_id"] == "run-1"       # 为未来的工作流预留
    assert set(payload) >= {"id", "attempts", "artifacts", "error_kind", "duration"}


def test_job_status_finished_helper():
    assert JobStatus.SUCCEEDED.finished
    assert JobStatus.FAILED.finished
    assert JobStatus.CANCELED.finished
    assert not JobStatus.RUNNING.finished


# --------------------------------------------------------------------------- 状态源

async def test_app_state_set_and_subscribe():
    state = AppState()
    seen: list[tuple[str, dict]] = []
    state.subscribe(lambda section, payload: seen.append((section, payload)))

    state.set("generation", status="running", progress=10)
    state.set("generation", status="running", progress=10)     # 值没变：不重复通知
    state.set("history", count=3)

    assert state.get("generation", "status") == "running"
    assert [section for section, _ in seen] == ["generation", "history"]


async def test_app_state_tracks_job_events():
    """界面只订阅状态，不轮询任务——这就是落点。"""
    state = AppState()
    job = Job(tool="image.generate")
    handle = JobHandle(job, asyncio.create_task(asyncio.sleep(0)))
    state.track(handle)

    assert state.get("generation", "status") == "pending"

    state.bus.emit(Event(type="job.started", job_id=job.id))
    assert state.get("generation", "status") == "running"

    state.bus.emit(Event(type="job.progress", job_id=job.id, payload={"progress": 40, "message": "生成中"}))
    assert state.get("generation", "progress") == 40
    assert state.get("generation", "message") == "生成中"

    state.bus.emit(Event(type="job.succeeded", job_id=job.id, payload={"result": {"url": "u"}}))
    assert state.get("generation", "status") == "succeeded"
    assert state.get("generation", "result") == {"url": "u"}
    assert state.get("generation", "progress") == 100

    state.bus.emit(Event(type="job.succeeded", job_id="other"))
    assert state.get("generation", "job_id") == job.id      # 只认自己的事件


async def test_app_state_tracks_failure_and_cancel():
    state = AppState()
    job = Job(tool="video.generate")
    handle = JobHandle(job, asyncio.create_task(asyncio.sleep(0)))
    state.track(handle)

    state.bus.emit(
        Event(type="job.failed", job_id=job.id, payload={"error": "认证失败", "error_kind": "auth"})
    )
    assert state.get("generation", "status") == "failed"
    assert state.get("generation", "error_kind") == "auth"

    state.bus.emit(Event(type="job.canceled", job_id=job.id))
    assert state.get("generation", "status") == "canceled"


async def test_app_state_clear_tracking_stops_updates():
    state = AppState()
    job = Job(tool="image.generate")
    handle = JobHandle(job, asyncio.create_task(asyncio.sleep(0)))
    state.track(handle)
    state.clear_tracking()

    state.bus.emit(Event(type="job.succeeded", job_id=job.id))
    assert state.get("generation", "status") == "pending"
