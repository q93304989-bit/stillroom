"""生成编排：图片/视频全流程、重试策略、取消、限流接入、历史落盘。

全部走 httpx.MockTransport + 虚拟时钟，测试不等待、不联网。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.capabilities.registry import build_registry
from app.clients.image_client import ImageRequest
from app.clients.video_client import VideoRequest
from app.config.credentials import (
    AgnesCredentials,
    Credentials,
    GitHubCredentials,
    LlmCredentials,
    SeeCredentials,
)
from app.net.http import HttpClient
from app.services.clock import VirtualClock
from app.services.generation import GenerationService
from app.services.history import HistoryStore
from app.services.media import MediaStore
from app.services.rate_limit import RateLimiter
from app.state.jobs import JobStatus

IMAGE_URL = "https://api.test/v1/images/generations"
VIDEO_URL = "https://api.test/v1/videos"
QUERY_URL = "https://api.test/agnesapi"
CDN = "https://cdn.test/a.png"


class Seq:
    """按顺序返回预置响应；用完后返回 500，便于暴露「多调了一次」。"""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, _request: httpx.Request, _n: int) -> httpx.Response:
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(500, json={"message": "unexpected extra call"})


class GateClock:
    """sleep 会一直等到测试放行——用来精确控制「轮询中途取消」。"""

    def __init__(self) -> None:
        self._now = 0.0
        self.slept: list[float] = []
        self.gate = asyncio.Event()

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await self.gate.wait()

    def release(self) -> None:
        self.gate.set()


def make_service(
    tmp_path: Path,
    routes: list,
    *,
    clock=None,
    history: bool = True,
    media: bool = True,
    **kwargs,
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for matcher, responder in routes:
            if matcher(request):
                return responder(request, len(seen))
        return httpx.Response(404, json={"message": f"no route for {request.url}"})

    credentials = Credentials(
        agnes=AgnesCredentials(
            api_key="sk-test",
            base_url="https://api.test/v1",
            image_model="agnes-image-2.5-flash",
            video_model="agnes-video-2.5-flash",
        ),
        github=GitHubCredentials(),
        see=SeeCredentials(),
        llm=LlmCredentials(),
    )
    http = HttpClient(transport=httpx.MockTransport(handler), backoff=0)
    registry = build_registry(http=http, credentials=credentials)
    clock = clock or VirtualClock()
    store = HistoryStore(tmp_path / "history.db") if history else None
    media_store = MediaStore(tmp_path / "data") if media else None
    service = GenerationService(
        registry=registry,
        credentials=credentials,
        http=http,
        history=store,
        media=media_store,
        limiter=RateLimiter(clock).configure_from(registry.rate_limits()),
        clock=clock,
        poll_interval=5.0,
        **kwargs,
    )
    return service, seen, store, clock


def by_url(method: str, url: str):
    return lambda request: request.method == method and str(request.url).split("?")[0] == url


def events_of(service, job_id: str, type_: str) -> list:
    return [e for e in service.bus.recent(job_id=job_id, limit=200) if e.type == type_]


# --------------------------------------------------------------------------- 图片

async def test_image_success_caches_media_and_writes_history(tmp_path):
    service, seen, history, _ = make_service(
        tmp_path,
        [
            (by_url("POST", IMAGE_URL), Seq(httpx.Response(200, json={"data": [{"url": CDN}]}))),
            (by_url("GET", CDN), Seq(httpx.Response(200, content=b"\x89PNG-bytes"))),
        ],
    )

    handle = service.start_image(ImageRequest(prompt="一只猫", size="512x512"))
    job = await handle.wait()

    assert job.status is JobStatus.SUCCEEDED
    assert job.result["url"] == CDN
    assert Path(job.result["media_path"]).read_bytes() == b"\x89PNG-bytes"

    record = history.list()[0]
    assert record.kind == "image"
    assert record.status == "success"
    assert record.prompt == "一只猫"
    assert record.result_url == CDN
    assert record.media_path == job.result["media_path"]
    assert record.params["size"] == "512x512"
    assert record.job_id == job.id

    types = [e.type for e in service.bus.recent(job_id=job.id, limit=50)]
    assert types == [
        "job.created",
        "job.started",
        "tool.called",
        "tool.returned",
        "tool.called",       # media.fetch
        "tool.returned",
        "artifact.created",
        "job.succeeded",
    ]


async def test_image_retries_transient_server_error(tmp_path):
    images = Seq(
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(200, json={"data": [{"url": CDN}]}),
    )
    service, _, _, clock = make_service(
        tmp_path,
        [
            (by_url("POST", IMAGE_URL), images),
            (by_url("GET", CDN), Seq(httpx.Response(200, content=b"x"))),
        ],
    )

    job = await service.start_image(ImageRequest(prompt="x")).wait()

    assert job.status is JobStatus.SUCCEEDED
    assert job.attempts == 2
    assert images.calls == 2
    retrying = events_of(service, job.id, "job.retrying")
    assert len(retrying) == 1
    assert retrying[0].get("error_kind") == "server"
    assert clock.total_slept == 2.0          # 首次退避 = 2s


async def test_image_auth_error_fails_immediately_without_retry(tmp_path):
    images = Seq(httpx.Response(401, json={"message": "Invalid token"}))
    service, seen, history, clock = make_service(
        tmp_path, [(by_url("POST", IMAGE_URL), images)]
    )

    job = await service.start_image(ImageRequest(prompt="x")).wait()

    assert job.status is JobStatus.FAILED
    assert job.error_kind == "auth"
    assert job.attempts == 1
    assert images.calls == 1
    assert clock.total_slept == 0            # 认证类硬故障不退避
    assert len(seen) == 1
    assert "apihub.agnes-ai.com" in job.error       # 提示里给出两个站点
    assert history.list()[0].status == "failed"


async def test_image_succeeds_even_if_local_cache_fails(tmp_path):
    service, _, history, _ = make_service(
        tmp_path,
        [
            (by_url("POST", IMAGE_URL), Seq(httpx.Response(200, json={"data": [{"url": CDN}]}))),
            (by_url("GET", CDN), Seq(httpx.Response(500, json={"message": "cdn down"}))),
        ],
    )

    job = await service.start_image(ImageRequest(prompt="x")).wait()

    assert job.status is JobStatus.SUCCEEDED
    assert job.result["url"] == CDN
    assert job.result["media_path"] is None
    assert history.list()[0].media_path is None
    messages = [e.get("message", "") for e in events_of(service, job.id, "job.progress")]
    assert any("缓存失败" in m for m in messages)


# --------------------------------------------------------------------------- 视频

async def test_video_success_flow_with_progress(tmp_path):
    queries = Seq(
        httpx.Response(200, json={"status": "queued", "progress": 0}),
        httpx.Response(200, json={"status": "running", "progress": 50}),
        httpx.Response(
            200,
            json={"status": "completed", "progress": 100, "url": "https://cdn.test/v.mp4",
                  "seconds": "8", "size": "1280x720", "quality": "standard"},
        ),
    )
    service, seen, history, clock = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), Seq(httpx.Response(200, json={"video_id": "vid-1"}))),
            (by_url("GET", QUERY_URL), queries),
        ],
    )

    job = await service.start_video(
        VideoRequest(prompt="海面日落", seconds="8", aspect_ratio="16:9")
    ).wait()

    assert job.status is JobStatus.SUCCEEDED
    assert job.result["url"] == "https://cdn.test/v.mp4"
    assert job.result["video_id"] == "vid-1"
    assert job.result["seconds"] == "8"
    assert queries.calls == 3
    assert clock.total_slept == 15.0          # 3 次轮询 × 5s

    progress = [e.get("progress") for e in events_of(service, job.id, "job.progress")]
    # 第一条是「任务已提交」的进度事件（此时还没有服务端进度）
    assert progress == [None, 0, 50, 100]

    record = history.list()[0]
    assert record.kind == "video"
    assert record.meta["video_id"] == "vid-1"
    assert record.result_url == "https://cdn.test/v.mp4"
    # 默认不自动下载视频（旧版也是按需下载）
    assert record.media_path is None


async def test_video_poll_longer_than_the_loop_threshold(tmp_path):
    """轮询要 8 次才完成也必须成功（回归：循环检测曾把轮询当打转掐死）。

    这条用例是这次的真根因所在：原来所有视频用例都在 **3 次以内** 就返回 completed，
    恰好卡在 LoopBreaker 的阈值上，于是「轮询到第 4 次就失败」这个必现 bug 一直没被撞出来。
    真实视频要 1~3 分钟（12~36 次轮询），所以这里至少要盖过阈值。
    """
    queries = Seq(
        httpx.Response(200, json={"status": "queued", "progress": 0}),
        httpx.Response(200, json={"status": "running", "progress": 10}),
        httpx.Response(200, json={"status": "running", "progress": 20}),
        httpx.Response(200, json={"status": "running", "progress": 30}),
        httpx.Response(200, json={"status": "running", "progress": 40}),
        httpx.Response(200, json={"status": "running", "progress": 60}),
        httpx.Response(200, json={"status": "running", "progress": 80}),
        httpx.Response(200, json={"status": "completed", "progress": 100,
                                  "url": "https://cdn.test/v.mp4"}),
    )
    service, _, _, clock = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), Seq(httpx.Response(200, json={"video_id": "vid-8"}))),
            (by_url("GET", QUERY_URL), queries),
        ],
    )

    job = await service.start_video(VideoRequest(prompt="海面日落", seconds="8")).wait()

    assert job.status is JobStatus.SUCCEEDED, f"轮询超过阈值就失败了：{job.error}"
    assert queries.calls == 8
    assert job.result["url"] == "https://cdn.test/v.mp4"
    assert clock.total_slept == 5.0 * 8          # 8 次轮询 × 5s，一次都没被提前打断


async def test_video_poll_repeat_is_not_a_model_loop(tmp_path):
    """轮询的「同工具同参数」重复不算模型打转：它绕开循环检测，但仍共享预算。

    反向保证也很重要——模型自己反复用同样的参数调一个工具时，循环检测必须照旧生效
    （见 tests/test_middleware.py 的 LoopBreaker 用例）。
    """
    queries = Seq(*[httpx.Response(200, json={"status": "running", "progress": n})
                    for n in range(0, 60)], 
                  httpx.Response(200, json={"status": "completed", "url": "https://cdn.test/v.mp4"}))
    service, _, _, _ = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), Seq(httpx.Response(200, json={"video_id": "vid-9"}))),
            (by_url("GET", QUERY_URL), queries),
        ],
    )

    job = await service.start_video(VideoRequest(prompt="x")).wait()

    assert job.status is JobStatus.SUCCEEDED, f"长时间轮询被误判成循环：{job.error}"
    assert queries.calls == 61                   # 远超阈值 3，仍然每次都放行


async def test_video_submit_retries_when_queue_full(tmp_path):
    submits = Seq(
        httpx.Response(503, json={"error": "video_queue_full"}),
        httpx.Response(200, json={"video_id": "vid-2"}),
    )
    service, _, _, clock = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), submits),
            (by_url("GET", QUERY_URL), Seq(httpx.Response(200, json={"status": "completed", "url": "u"}))),
        ],
    )

    job = await service.start_video(VideoRequest(prompt="x")).wait()

    assert job.status is JobStatus.SUCCEEDED
    assert submits.calls == 2
    # 10s 队列满退避 + 50s 重试提交又撞上「每分钟 1 个」的平台限流 + 5s 轮询间隔
    assert clock.total_slept == 65.0
    retrying = events_of(service, job.id, "job.retrying")
    assert retrying[0].get("error_kind") == "queue_full"


async def test_video_poll_tolerates_registration_delay(tmp_path):
    """拥堵期查询会先返回 404（任务还没注册），必须继续等而不是失败。"""
    queries = Seq(
        httpx.Response(404, json={"message": "任务不存在"}),
        httpx.Response(404, json={"message": "任务不存在"}),
        httpx.Response(200, json={"status": "completed", "url": "u"}),
    )
    service, _, _, clock = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), Seq(httpx.Response(200, json={"video_id": "vid-3"}))),
            (by_url("GET", QUERY_URL), queries),
        ],
    )

    job = await service.start_video(VideoRequest(prompt="x")).wait()

    assert job.status is JobStatus.SUCCEEDED
    assert queries.calls == 3
    assert clock.total_slept == 5.0 * 3 + 10.0 * 2      # 轮询间隔 + 两次 404 退避


async def test_video_poll_auth_error_fails_immediately(tmp_path):
    queries = Seq(httpx.Response(401, json={"message": "Invalid token"}))
    service, _, _, clock = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), Seq(httpx.Response(200, json={"video_id": "vid-4"}))),
            (by_url("GET", QUERY_URL), queries),
        ],
    )

    job = await service.start_video(VideoRequest(prompt="x")).wait()

    assert job.status is JobStatus.FAILED
    assert job.error_kind == "auth"
    assert queries.calls == 1                # 绝不退避 90 次（旧版的 15 分钟白等）
    assert clock.total_slept == 5.0


async def test_video_task_failed_status_is_reported(tmp_path):
    service, _, history, _ = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), Seq(httpx.Response(200, json={"video_id": "vid-5"}))),
            (
                by_url("GET", QUERY_URL),
                Seq(httpx.Response(200, json={"status": "failed", "error": "内容审核未通过"})),
            ),
        ],
    )

    job = await service.start_video(VideoRequest(prompt="x")).wait()

    assert job.status is JobStatus.FAILED
    assert "内容审核未通过" in job.error
    assert history.list()[0].status == "failed"


async def test_video_cancel_during_polling_stops_queries(tmp_path):
    gate = GateClock()
    queries = Seq(
        httpx.Response(200, json={"status": "queued", "progress": 0}),
        httpx.Response(200, json={"status": "running", "progress": 10}),
    )
    service, _, history, _ = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), Seq(httpx.Response(200, json={"video_id": "vid-6"}))),
            (by_url("GET", QUERY_URL), queries),
        ],
        clock=gate,
    )

    handle = service.start_video(VideoRequest(prompt="x"))
    await asyncio.sleep(0)          # 让提交跑完
    await asyncio.sleep(0)
    assert queries.calls == 0       # 提交后先睡一个轮询间隔

    handle.cancel()
    job = await handle.wait()

    assert job.status is JobStatus.CANCELED
    assert "服务端任务可能仍在运行" in job.message
    assert queries.calls == 0
    assert history.list()[0].status == "failed"
    assert events_of(service, job.id, "job.canceled")


async def test_video_submit_is_rate_limited_by_capability_metadata(tmp_path):
    """限流器由能力元数据配置，第二次提交会排队等待。"""
    service, _, _, clock = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), Seq(
                httpx.Response(200, json={"video_id": "a"}),
                httpx.Response(200, json={"video_id": "b"}),
            )),
            (by_url("GET", QUERY_URL), Seq(
                httpx.Response(200, json={"status": "completed", "url": "u1"}),
                httpx.Response(200, json={"status": "completed", "url": "u2"}),
            )),
        ],
    )

    assert service.limiter.limit_of("video.submit") == 1     # 来自 ToolSpec.rate_limit

    first = await service.start_video(VideoRequest(prompt="a")).wait()
    second = await service.start_video(VideoRequest(prompt="b")).wait()

    assert first.status is JobStatus.SUCCEEDED
    assert second.status is JobStatus.SUCCEEDED
    throttled = [
        e.get("throttled_seconds")
        for e in events_of(service, second.id, "tool.called")
        if e.get("tool_name") == "video.submit"
    ]
    assert throttled and throttled[0] > 0        # 第二次提交被排队


# --------------------------------------------------------------------------- 通用入口

async def test_generic_tool_job_records_parent_id(tmp_path):
    """未来工作流的接入点：按能力名调用，并带上父节点。"""
    service, _, _, _ = make_service(
        tmp_path,
        [(by_url("POST", IMAGE_URL), Seq(httpx.Response(200, json={"data": [{"url": CDN}]})))],
        media=False,
    )

    handle = service.start_tool(
        "image.generate", {"prompt": "x", "size": "512x512"}, parent_job_id="run-1"
    )
    job = await handle.wait()

    assert job.status is JobStatus.SUCCEEDED
    assert job.parent_job_id == "run-1"
    assert service.handle(job.id) is handle
    assert service.running_jobs == ()


# --------------------------------------------------------------------------- 队列满与额度

async def test_queue_full_retries_do_not_eat_the_budget(tmp_path):
    """平台 503（队列满）重试**不该消耗额度**，更不能因此报「额度已耗尽」。

    真人实测的现场：video.submit 上限 2，平台 503 重试两次就把额度吃光，
    用户看到的是「本次运行已用完 video.submit 的额度」——真实原因（队列满）被彻底掩盖。
    而平台校验失败并不会创建任务、也不产生费用，我方却先把自己的额度用光了。
    """
    submits = Seq(
        httpx.Response(503, json={"error": "video_queue_full"}),
        httpx.Response(503, json={"error": "video_queue_full"}),
        httpx.Response(200, json={"video_id": "vid-budget"}),
    )
    service, _, _, _ = make_service(
        tmp_path,
        [
            (by_url("POST", VIDEO_URL), submits),
            (by_url("GET", QUERY_URL), Seq(httpx.Response(200, json={"status": "completed", "url": "u"}))),
        ],
    )

    job = await service.start_video(VideoRequest(prompt="雨夜霓虹小巷", seconds="5")).wait()

    assert job.status is JobStatus.SUCCEEDED, f"队列满重试把额度吃光了：{job.error}"
    assert submits.calls == 3
    usage = job.context.get("usage") or {}
    assert usage.get("video.submit") == 1, f"重试不该再加额度计数，实际 {usage}"


async def test_queue_full_that_persists_reports_the_real_reason(tmp_path):
    """平台一直 503 时，报的必须是「队列已满」，不是「额度已耗尽」。"""
    submits = Seq(*[httpx.Response(503, json={"error": "video_queue_full"}) for _ in range(10)])
    service, _, _, _ = make_service(tmp_path, [(by_url("POST", VIDEO_URL), submits)])

    job = await service.start_video(VideoRequest(prompt="x", seconds="5")).wait()

    assert job.status is JobStatus.FAILED
    assert job.error_kind == "queue_full", f"原因被说成 {job.error_kind}：{job.error}"
    assert "队列" in job.error
    assert "额度" not in job.error, "队列满不该说成额度问题"
    assert submits.calls == 3                     # 受 retry_attempts=3 控制
