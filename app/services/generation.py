"""生成编排：把「一次生成」从提交跑到落盘。

这一层是旧版 god class 里最核心的那部分逻辑的正规化版本，差异在于：

- **限流前置**：提交前先按能力元数据里的限额排队（视频每分钟 1 个），不再靠「失败后
  退避 60 秒」来绕；
- **重试有依据**：只重试 `AppError.retryable` 的类型，认证类硬故障立刻失败
  （旧版曾把 401 退了 90 次、白等 15 分钟）；
- **可取消**：任务句柄化，取消会真的中断等待与网络请求；
- **可观察**：每一步都发结构化事件，界面订阅即可，无需轮询；
- **有产出**：成功/失败都写进历史，产物落地到媒体目录。

时间处理：Job 上的时间戳用挂钟（给人看、给历史用），调度用的等待用注入的 `Clock`
（单调时钟；测试里是虚拟时钟）。
"""

from __future__ import annotations

import asyncio
import time
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from app.capabilities.middleware import CONTEXT_BUDGET_FREE, CONTEXT_HISTORY
from app.capabilities.registry import ToolRegistry
from app.clients.image_client import ImageRequest
from app.clients.video_client import (
    DONE_STATUSES,
    FAILED_STATUSES,
    VideoRequest,
    extract_video_id,
)
from app.config.credentials import Credentials
from app.net.errors import (
    AppError,
    AuthError,
    NotFoundError,
    QueueFullError,
    RateLimitError,
    ResponseFormatError,
)
from app.net.http import HttpClient
from app.services.clock import Clock, SystemClock
from app.services.history import HistoryStore, Record
from app.services.media import MediaStore, guess_ext
from app.services.rate_limit import RateLimiter
from app.state.events import Event, EventBus
from app.state.jobs import Job, JobHandle, JobStatus


class GenerationService:
    """生成任务编排器。一个实例服务整个应用，所有入口都返回 `JobHandle`。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        credentials: Credentials,
        http: HttpClient,
        bus: EventBus | None = None,
        history: HistoryStore | None = None,
        media: MediaStore | None = None,
        limiter: RateLimiter | None = None,
        clock: Clock | None = None,
        poll_interval: float = 5.0,
        retry_attempts: int = 3,
        video_max_polls: int = 480,
        cache_images: bool = True,
        cache_videos: bool = False,
    ) -> None:
        self.registry = registry
        self.credentials = credentials
        self._http = http
        self.bus = bus or EventBus()
        self.history = history
        self.media = media
        self.clock = clock or SystemClock()
        self.limiter = limiter or RateLimiter(self.clock).configure_from(
            registry.rate_limits()
        )
        self.poll_interval = poll_interval
        self.retry_attempts = max(1, retry_attempts)
        self.video_max_polls = video_max_polls
        self.cache_images = cache_images
        self.cache_videos = cache_videos
        self._handles: dict[str, JobHandle] = {}

    # ---------------------------------------------------------------- 入口

    @property
    def running_jobs(self) -> tuple[JobHandle, ...]:
        return tuple(h for h in self._handles.values() if not h.done)

    def handle(self, job_id: str) -> JobHandle | None:
        return self._handles.get(job_id)

    def start_image(
        self,
        request: ImageRequest,
        *,
        context: Mapping[str, Any] | None = None,
        parent_job_id: str | None = None,
    ) -> JobHandle:
        """出图。`context` 传入时与调用方共享预算与调用历史（agent 运行时用）。"""
        params = {
            "prompt": request.prompt,
            "images": list(request.images or ()),
            "model": request.model or self.credentials.agnes.image_model,
            "size": request.size,
        }
        return self._spawn(
            "image.generate", params, self._run_image,
            context=context, parent_job_id=parent_job_id,
        )

    def start_video(
        self,
        request: VideoRequest,
        *,
        context: Mapping[str, Any] | None = None,
        parent_job_id: str | None = None,
    ) -> JobHandle:
        params = {
            "prompt": request.prompt,
            "images": list(request.images or ()),
            "model": request.model or self.credentials.agnes.video_model,
            "seconds": str(request.seconds),
            "aspect_ratio": request.aspect_ratio,
            "seed": request.seed,
        }
        return self._spawn(
            "video.generate", params, self._run_video,
            context=context, parent_job_id=parent_job_id,
        )

    def start_tool(
        self, tool: str, params: Mapping[str, Any], *, parent_job_id: str | None = None
    ) -> JobHandle:
        """通用入口：未来的确定性工作流按能力名逐个调用，无需新增代码。"""
        return self._spawn(
            tool, dict(params), self._run_generic, parent_job_id=parent_job_id
        )

    # ---------------------------------------------------------------- 任务骨架

    def _spawn(
        self,
        tool: str,
        params: Mapping[str, Any],
        runner: Callable[[Job], Awaitable[Job]],
        *,
        parent_job_id: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> JobHandle:
        job = Job(
            tool=tool,
            params=dict(params),
            parent_job_id=parent_job_id,
            context=dict(context) if context is not None else {},
        )
        task = asyncio.create_task(runner(job))
        handle = JobHandle(job, task)
        self._handles[job.id] = handle
        # 兜底：任务若在**第一次执行之前**就被取消，协程里一行代码都不会跑（连
        # `except asyncio.CancelledError` 都没机会执行），状态会永远停在「排队中」。
        # 实测过：点完「生成」立刻点取消，取消落在这一段就会静默失效。
        task.add_done_callback(partial(self._settle_never_started, job=job))
        self._emit("job.created", job, tool=tool, params_summary=_summarize(params))
        return handle

    def _settle_never_started(self, task: asyncio.Task, *, job: Job) -> None:
        """收尾「还没开始就被取消」的任务（正常取消由任务自己处理，这里不插手）。

        补两样东西：终态事件（界面才不会再停在「排队中」）与历史记录（取消也要留痕，
        与正常取消路径写的是同一种记录）。
        """
        if not task.cancelled() or job.started_at is not None:
            return
        if job.status is not JobStatus.PENDING:
            return
        self._cancel(job, message="已取消")
        asyncio.create_task(
            self._write_history(job, status="failed", error="已取消", error_kind="canceled")
        )

    def _emit(self, type_: str, job: Job, **payload: Any) -> Event:
        base = {"tool": job.tool, "status": job.status.value}
        base.update(payload)
        return self.bus.emit(Event(type=type_, job_id=job.id, payload=base))

    def _begin(self, job: Job) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        self._emit("job.started", job)

    def _succeed(self, job: Job, *, message: str = "") -> Job:
        job.status = JobStatus.SUCCEEDED
        job.finished_at = time.time()
        job.message = message or job.message
        if job.progress is None:
            job.progress = 100
        self._emit("job.succeeded", job, message=job.message, result=job.result)
        return job

    def _fail(self, job: Job, error: AppError) -> Job:
        job.status = JobStatus.FAILED
        job.finished_at = time.time()
        job.error = error.user_message
        job.error_kind = error.kind
        self._emit(
            "job.failed", job, error=job.error, error_kind=job.error_kind, retryable=error.retryable
        )
        return job

    def _cancel(self, job: Job, *, message: str = "已取消") -> Job:
        job.status = JobStatus.CANCELED
        job.finished_at = job.finished_at or time.time()
        job.message = message
        self._emit("job.canceled", job, message=message)
        return job

    # ---------------------------------------------------------------- 调用与重试

    async def _invoke(
        self, job: Job, tool: str, params: Mapping[str, Any], *, scoped: bool = True
    ) -> Any:
        """限流 → 闸门 → 调用 → 验收 → 事件。所有对外调用都必须走这里。

        `job.context` 是这次运行共享的上下文：预算计数与调用历史放在里面，
        所以「一次任务最多出 6 张」「同样参数别试第 4 次」这类规则才有「一次运行」的概念。

        `scoped=False` 用于**本服务自己的编排调用**（下载产物、轮询视频状态）：
        这些动作是代码决定的，不是模型选择的，所以不看阶段白名单——否则 agent 运行时
        在「生成」阶段调用出图，反而会被自己的下载调用撞上白名单而拿不到本地文件
        （实测 bug：图出来了，但本地缓存与缩略图静默丢失）。
        **预算照旧共享**（它算的是「这次运行花了多少」，必须跨调用累计）；
        **循环检测不共享**：轮询天生就是「同工具 + 同参数」反复调用，把它算成
        「模型在打转」会让每一次视频生成在第 4 次查询（约 15 秒）准时失败——
        真实出片要几十次轮询，所以这条路会必然挂掉。代码自己决定的重复调用不是打转。
        """
        context = job.context
        if not scoped:
            context = {
                key: value
                for key, value in job.context.items()
                if key not in ("allowed_tools", CONTEXT_HISTORY)
            }
        waited = await self.limiter.acquire(tool)
        self._emit(
            "tool.called",
            job,
            tool_name=tool,
            throttled_seconds=round(waited, 3),
            params_summary=_summarize(params),
        )
        result = await self.registry.invoke(tool, params, context=context)
        self._emit("tool.returned", job, tool_name=tool)
        return result

    def _retry_delay(self, error: AppError, attempt: int) -> float:
        if isinstance(error, RateLimitError):
            return error.retry_after or 60.0        # 平台限制：等满一个窗口
        if isinstance(error, QueueFullError):
            return 10.0 * attempt                   # 免费通道拥堵，线性退避
        if isinstance(error, NotFoundError):
            return 10.0                             # 任务尚未注册
        return min(30.0, 2.0 * attempt)

    async def _call_with_retry(
        self,
        job: Job,
        call: Callable[[], Awaitable[Any]],
        *,
        tool: str,
        attempts: int | None = None,
    ) -> Any:
        """只重试 `retryable` 的错误；认证类硬故障立即上抛。

        `tool` 是被调用的能力名（**不是 `job.tool`**）：视频任务的 job 叫 `video.generate`，
        真正消耗额度的是它提交的 `video.submit`——按 job.tool 标记会标错对象。

        每次重试前把该工具标进 `CONTEXT_BUDGET_FREE`：**重试不该消耗额度**。
        理由：平台 503 / 队列满时重试的是同一个动作，不是我方额外多干了一件事；
        把重试算进额度会让用户撞上「平台排队 → 额度被自己吃光 → 报『额度已耗尽』」。
        标记是一次性的（`BudgetGate` 放行后即清），所以额度不会因此变成无限。
        """
        limit = attempts or self.retry_attempts
        last: AppError | None = None
        for attempt in range(1, limit + 1):
            job.attempts = attempt
            try:
                return await call()
            except AppError as error:
                last = error
                if not error.retryable or attempt >= limit:
                    raise
                delay = self._retry_delay(error, attempt)
                self._emit(
                    "job.retrying",
                    job,
                    attempt=attempt,
                    delay=delay,
                    error_kind=error.kind,
                    message=f"{error.message}（{delay:.0f}s 后第 {attempt + 1} 次尝试）",
                )
                # 下一次是重试：免一次额度（一次性标记，BudgetGate 放行后即清）
                job.context.setdefault(CONTEXT_BUDGET_FREE, set()).add(str(tool))
                await self.clock.sleep(delay)
        raise last or AppError("调用失败")

    # ---------------------------------------------------------------- 图片

    async def _run_image(self, job: Job) -> Job:
        self._begin(job)
        params = {
            "prompt": job.params.get("prompt", ""),
            "images": tuple(job.params.get("images") or ()),
            "model": job.params.get("model", ""),
            "size": job.params.get("size", ""),
        }
        try:
            url = await self._call_with_retry(
                job,
                lambda: self._invoke(job, "image.generate", params),
                tool="image.generate",
            )
            media_path: Path | None = None
            if self.cache_images and url:
                try:
                    media_path = await self._download(job, url, kind="image")
                except AppError as exc:
                    # 缓存失败不影响「图已生成」这个事实
                    self._emit(
                        "job.progress", job, message=f"图片已生成，本地缓存失败：{exc.message}"
                    )
            job.result = {
                "url": url,
                "media_path": str(media_path) if media_path else None,
            }
            self._succeed(job, message="图片已生成")
            record = await self._write_history(
                job, status="success", result_url=url, media_path=media_path
            )
            await self._thumbnail_for(record, media_path)
        except asyncio.CancelledError:
            self._cancel(job)
            await self._write_history(job, status="failed", error="已取消", error_kind="canceled")
            raise
        except AppError as error:
            self._fail(job, error)
            await self._write_history(job, status="failed", error=job.error, error_kind=error.kind)
        return job

    # ---------------------------------------------------------------- 视频

    async def _run_video(self, job: Job) -> Job:
        self._begin(job)
        params = {
            "prompt": job.params.get("prompt", ""),
            "images": tuple(job.params.get("images") or ()),
            "model": job.params.get("model", ""),
            "seconds": str(job.params.get("seconds") or "5"),
            "aspect_ratio": str(job.params.get("aspect_ratio") or "16:9"),
        }
        try:
            submitted = await self._call_with_retry(
                job,
                lambda: self._invoke(job, "video.submit", params),
                tool="video.submit",
            )
            video_id = extract_video_id(submitted)
            if not video_id:
                raise ResponseFormatError(f"提交成功但没拿到 video_id：{str(submitted)[:150]}")
            self._emit("job.progress", job, message=f"任务已提交：{video_id}", video_id=video_id)

            final = await self._poll_video(job, video_id, str(params["model"]))
            url = str(final.get("url") or "")
            if not url:
                raise ResponseFormatError(f"任务完成但没有视频地址：{str(final)[:150]}")

            media_path: Path | None = None
            if self.cache_videos:
                try:
                    media_path = await self._download(job, url, kind="video")
                except AppError as exc:
                    self._emit("job.progress", job, message=f"视频已生成，本地缓存失败：{exc.message}")

            job.result = {
                "url": url,
                "video_id": video_id,
                "seconds": final.get("seconds"),
                "size": final.get("size"),
                "quality": final.get("quality"),
                "media_path": str(media_path) if media_path else None,
            }
            self._succeed(job, message="视频已生成")
            record = await self._write_history(
                job,
                status="success",
                result_url=url,
                media_path=media_path,
                meta={
                    "video_id": video_id,
                    "seconds": final.get("seconds"),
                    "size": final.get("size"),
                    "quality": final.get("quality"),
                },
            )
            await self._thumbnail_for(record, media_path)
        except asyncio.CancelledError:
            self._cancel(job, message="已取消（服务端任务可能仍在运行）")
            await self._write_history(job, status="failed", error="已取消", error_kind="canceled")
            raise
        except AppError as error:
            self._fail(job, error)
            await self._write_history(
                job, status="failed", error=job.error, error_kind=error.kind,
                meta={"video_id": job.result.get("video_id") if isinstance(job.result, dict) else None},
            )
        return job

    async def _poll_video(self, job: Job, video_id: str, model_name: str) -> dict:
        """轮询直到结束。节奏与退避策略集中在这里，客户端本身不 sleep。"""
        consecutive_errors = 0
        for index in range(1, self.video_max_polls + 1):
            await self.clock.sleep(self.poll_interval)
            try:
                status = await self._invoke(
                    job,
                    "video.query",
                    {"video_id": video_id, "model_name": model_name},
                    scoped=False,
                )
                consecutive_errors = 0
            except AuthError:
                raise                                   # 配置类硬故障：立刻失败
            except RateLimitError as error:
                consecutive_errors += 1
                if consecutive_errors >= 6:
                    raise AppError(
                        f"查询被持续限流，请稍后再查；任务 {video_id} 仍在服务端保留"
                    ) from error
                delay = error.retry_after or min(30.0, 10.0 * consecutive_errors)
                self._emit("job.progress", job, message="查询被限流，退避中…")
                await self.clock.sleep(delay)
                continue
            except AppError as error:
                if not error.retryable:
                    raise
                consecutive_errors += 1
                if consecutive_errors >= 90:
                    raise AppError(
                        f"查询任务持续失败（已重试约 15 分钟）；任务 {video_id} 可能仍在服务端运行"
                    ) from error
                self._emit(
                    "job.progress",
                    job,
                    message=f"任务排队注册中（第 {consecutive_errors} 次重试）…",
                )
                await self.clock.sleep(self._retry_delay(error, consecutive_errors))
                continue

            state = str(status.get("status", "unknown"))
            progress = status.get("progress")
            job.progress = progress if isinstance(progress, (int, float)) else job.progress
            self._emit(
                "job.progress",
                job,
                progress=progress,
                remote_status=state,
                message=f"视频{_status_cn(state)}" + (f" · {progress}%" if progress is not None else ""),
            )
            if state in DONE_STATUSES:
                if state in FAILED_STATUSES:
                    detail = status.get("error") or status.get("message") or status
                    raise AppError(f"视频生成失败：{detail}")
                return status
        raise AppError("视频生成超时，可稍后用 video_id 重新查询")

    # ---------------------------------------------------------------- 通用工具任务

    async def _run_generic(self, job: Job) -> Job:
        self._begin(job)
        try:
            result = await self._call_with_retry(
                job,
                lambda: self._invoke(job, job.tool, job.params),
                tool=str(job.tool),
            )
            job.result = result
            self._succeed(job)
        except asyncio.CancelledError:
            self._cancel(job)
            raise
        except AppError as error:
            self._fail(job, error)
        return job

    # ---------------------------------------------------------------- 产物与历史

    async def _download(self, job: Job, url: str, *, kind: str) -> Path | None:
        if self.media is None:
            return None
        data = await self._invoke(job, "media.fetch", {"url": url, "timeout": 120}, scoped=False)
        path = self.media.save_bytes(data, job.id, ext=guess_ext(url, kind))
        job.artifacts.append(str(path))
        self._emit("artifact.created", job, kind=kind, path=str(path), bytes=len(data))
        return path

    async def _write_history(
        self,
        job: Job,
        *,
        status: str,
        result_url: str | None = None,
        media_path: Path | None = None,
        error: str | None = None,
        error_kind: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> Record | None:
        if self.history is None:
            return None
        params = dict(job.params)
        kind = "video" if job.tool == "video.generate" else "image"
        record = Record(
            kind=kind,
            status=status,
            prompt=str(params.get("prompt", "")),
            params=params,
            refs=[str(x) for x in (params.get("images") or ())],
            result_url=result_url,
            media_path=str(media_path) if media_path else None,
            error=error,
            meta={**(dict(meta or {})), "job_id": job.id, "error_kind": error_kind},
            job_id=job.id,
            duration=job.duration,
        )
        # SQLite 是同步 API，挪到线程里执行，避免阻塞事件循环
        return await asyncio.to_thread(self.history.add, record)

    async def _thumbnail_for(self, record: Record | None, media_path: Path | None) -> None:
        """落盘后顺手做一张缩略图。

        为什么要在这里做：否则历史页首次打开时，一屏几十张图都要现解，既慢又会让滚动抖动。
        生成时多做一次（几十毫秒，在后台线程）换的是「历史页一进来就是热的」。
        """
        if record is None or media_path is None or self.media is None or self.history is None:
            return
        try:
            thumb = await asyncio.to_thread(self.media.thumbnail_for, record.id, media_path)
            if thumb:
                await asyncio.to_thread(self.history.update, record.id, thumb_path=str(thumb))
        except Exception:
            # 缩略图失败不该影响生成结果：历史里没有缩略图，历史页会后台补
            pass


_STATUS_CN = {
    "queued": "排队中",
    "pending": "排队中",
    "in_progress": "生成中",
    "running": "生成中",
    "processing": "处理中",
    "completed": "已完成",
    "succeeded": "已完成",
    "success": "已完成",
    "failed": "失败",
    "error": "失败",
    "canceled": "已取消",
    "cancelled": "已取消",
}


def _status_cn(state: str) -> str:
    return _STATUS_CN.get(state.lower(), state)


def _summarize(params: Mapping[str, Any]) -> dict[str, Any]:
    """参数摘要：进事件与日志，绝不落密钥、绝不塞进 base64 全文。"""
    summary: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if key == "prompt":
            summary["prompt"] = f"<{len(str(value))} 字>"
        elif key == "images":
            items = list(value or [])
            summary["images"] = f"{len(items)} 张"
        elif key in ("api_key", "token", "authorization"):
            continue
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        else:
            summary[key] = type(value).__name__
    return summary
