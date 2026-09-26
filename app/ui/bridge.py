"""界面与服务层之间的唯一门面。

QML 只认这个对象：读属性、调槽函数、听信号。桥本身**不实现业务**——它翻译参数、
转发调用、把状态变化转成信号。

线程约定：

- QML 调用槽 → 在 Qt 主线程；
- 真正干活的协程 → 提交给 `AsyncRunner` 的 asyncio 线程；
- 状态与事件回调发生在 asyncio 线程 → 在这里 emit Qt 信号（Qt 会排队回主线程）。

于是「界面线程不做 IO」这条硬规则天然成立。
"""

from __future__ import annotations

import webbrowser
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication

from app.bootstrap import AppContext
from app.clients.image_client import DEFAULT_SIZE, ImageRequest
from app.clients.video_client import VideoRequest
from app.config.logs import log_error
from app.net.errors import AppError, ValidationError
from app.state.jobs import JobHandle
from app.ui.async_runner import AsyncRunner
from app.ui.models.history_model import HistoryModel
from app.ui.theme import Theme


@dataclass
class _View:
    """界面要展示的一份快照。"""

    job_id: str = ""
    tool: str = ""
    status: str = "idle"
    progress: float = -1.0
    message: str = ""
    result_url: str = ""
    result_local: str = ""
    error: str = ""
    error_kind: str = ""
    #: 等用户批准的**工具名**（如 image_host.upload）。非空时界面必须给一个确认出口——
    #: 否则用户点了按钮，只有一次转瞬即逝的提示，看起来就是「点了没反应」。
    pending_approval: str = ""


_STATUS_TEXT = {
    "idle": "空闲",
    "pending": "排队中",
    "running": "生成中",
    "succeeded": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}


class UiBridge(QObject):
    """QML 侧的唯一入口对象（注册为上下文属性 `backend`）。"""

    stateChanged = Signal()
    noticeRaised = Signal(str, str)          # level(info/warn/error), text
    referenceUploaded = Signal(str, str)     # 本地路径, 公网 URL
    jobStarted = Signal(str)                 # job_id
    jobFinished = Signal(str, bool, str)     # job_id, ok, message
    paramsLoaded = Signal(str, "QVariantMap")   # kind(image/video), params
    credentialsChanged = Signal()

    def __init__(
        self,
        context: AppContext,
        runner: AsyncRunner,
        theme: Theme,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._ctx = context
        self._runner = runner
        self._theme = theme
        self._view = _View()
        self._current: JobHandle | None = None
        #: 取消请求早于任务句柄登记时的暂存（见 `cancel`）
        self._cancel_requested = False
        #: 等用户确认上传的本地图片路径（见 `uploadReference` / `confirmUpload`）
        self._pending_upload = ""
        self._unsubscribe_state = context.state.subscribe(self._on_state_changed)
        self._history = HistoryModel(context, runner, parent=self)

    # ---------------------------------------------------------------- 属性

    def _get_view(self) -> _View:
        return self._view

    configured = Property(
        bool, lambda self: bool(self._ctx.credentials.agnes.api_key), notify=credentialsChanged
    )
    siteLabel = Property(
        str, lambda self: self._ctx.credentials.agnes.site, notify=credentialsChanged
    )
    apiBase = Property(
        str, lambda self: self._ctx.credentials.agnes.base_url, notify=credentialsChanged
    )
    dataDir = Property(str, lambda self: str(self._ctx.data_dir), constant=True)
    llmConfigured = Property(
        bool, lambda self: self._ctx.credentials.llm.configured, notify=credentialsChanged
    )

    @Slot()
    def refreshCredentials(self) -> None:       # noqa: N802 - 设置页保存后通知界面刷新
        """凭据已由设置页写回并重载，这里只负责让界面上的徽标跟着更新。"""
        self.credentialsChanged.emit()

    status = Property(str, lambda self: self._view.status, notify=stateChanged)
    statusText = Property(str, lambda self: _STATUS_TEXT.get(self._view.status, self._view.status), notify=stateChanged)
    toolName = Property(str, lambda self: self._view.tool, notify=stateChanged)
    #: 等用户确认的工具名（非空时界面必须显示确认出口，见 `_View.pending_approval`）
    pendingApproval = Property(  # noqa: N802 - QML 命名
        str, lambda self: self._view.pending_approval, notify=stateChanged
    )
    busy = Property(
        bool, lambda self: self._view.status in ("pending", "running"), notify=stateChanged
    )
    progress = Property(float, lambda self: self._view.progress, notify=stateChanged)
    message = Property(str, lambda self: self._view.message, notify=stateChanged)
    resultUrl = Property(str, lambda self: self._view.result_url, notify=stateChanged)
    resultLocalPath = Property(str, lambda self: self._view.result_local, notify=stateChanged)
    hasResult = Property(
        bool,
        lambda self: bool(self._view.result_url or self._view.result_local),
        notify=stateChanged,
    )
    resultSource = Property(str, lambda self: self._result_source(), notify=stateChanged)
    errorText = Property(str, lambda self: self._view.error, notify=stateChanged)
    errorKind = Property(str, lambda self: self._view.error_kind, notify=stateChanged)

    theme = Property(QObject, lambda self: self._theme, constant=True)
    historyModel = Property(QObject, lambda self: self._history, constant=True)

    # ---------------------------------------------------------------- 生成

    @Slot(str, str, str, "QVariantList")
    def generateImage(self, prompt: str, size: str, model: str, refs: list) -> None:
        """文生图 / 图生图。`refs` 是本地路径或公网 URL。"""
        if not (prompt or "").strip():
            self._notify("warn", "请先输入提示词")
            return
        if self.busy:
            self._notify("warn", "已有任务进行中，可先取消")
            return
        request = ImageRequest(
            prompt=prompt.strip(),
            size=(size or DEFAULT_SIZE).strip(),
            model=(model or "").strip(),
            images=tuple(str(item) for item in (refs or []) if str(item).strip()),
        )
        self._launch("image.generate", lambda: self._ctx.generation.start_image(request))

    @Slot(str, str, str, str, "QVariantList")
    def generateVideo(
        self, prompt: str, model: str, seconds: str, aspect: str, refs: list
    ) -> None:
        """文生视频 / 图参考视频。`refs` 必须是公网 URL。"""
        if not (prompt or "").strip():
            self._notify("warn", "请先输入提示词")
            return
        if self.busy:
            self._notify("warn", "已有任务进行中，可先取消")
            return
        request = VideoRequest(
            prompt=prompt.strip(),
            model=(model or "").strip(),
            seconds=str(seconds or "5"),
            aspect_ratio=(aspect or "16:9").strip(),
            images=tuple(str(item) for item in (refs or []) if str(item).strip()),
        )
        self._launch("video.generate", lambda: self._ctx.generation.start_video(request))

    @Slot()
    def cancel(self) -> None:
        """取消当前任务。

        **判定也要回到 asyncio 线程里做**：任务句柄是异步登记的——点「生成」时界面线程
        只把状态置成「排队中」，`_current` 要等协程跑到 `_start` 才有值。这段时间里点
        取消，如果在界面线程判定「没有当前任务」就返回，请求会被静静吞掉，任务照跑
        （实测：视频任务会一路轮询到出片，慢一点的机器上必现）。
        """
        if self._view.status not in ("pending", "running"):
            return
        self._notify("info", "已请求取消")
        self._runner.call(self._cancel_in_loop)

    def _cancel_in_loop(self) -> None:
        """在 asyncio 线程里判定并取消（因此判定顺序与句柄登记顺序一致）。"""
        handle = self._current
        if handle is not None and not handle.done:
            handle.cancel()          # 还没开始跑就被取消也安全：服务层会补终态
        else:
            self._cancel_requested = True

    @Slot()
    def clearResult(self) -> None:
        self._view = _View()
        self._current = None
        self.stateChanged.emit()

    # ---------------------------------------------------------------- 辅助操作

    @Slot(str)
    def uploadReference(self, path: str) -> None:
        """把本地图片传到图床（视频接口只吃公网 URL）。

        上传有 `upload` 副作用（内容会送到公网、不可回滚），所以审批闸门会拦一道。
        以前的写法是「先弹个提示就结束」——界面上没有任何确认出口，用户看到的就是
        「点了没反应」。现在分两步：先把待确认摆出来，用户点确认后再带 context 执行。
        """
        if not path:
            return
        self._pending_upload = str(path)
        self._view = replace(self._view, pending_approval="image_host.upload",
                             message="")
        self.stateChanged.emit()
        self._notify(
            "warn",
            "这张图会被上传到公网图床（不可撤回），确认后才会执行",
        )

    @Slot()
    def confirmUpload(self) -> None:
        """用户确认上传（`pending_approval` 的出口）。"""
        path = self._pending_upload
        if not path:
            self._notify("warn", "没有待确认的上传")
            return
        self._pending_upload = ""
        self._view = replace(self._view, pending_approval="")
        self.stateChanged.emit()

        async def _run() -> None:
            from app.capabilities.middleware import CONTEXT_APPROVED

            try:
                url = await self._ctx.registry.invoke(
                    "image_host.upload",
                    {"path": path},
                    # 带上「这一个工具已获批准」：闸门只放行这一项，别的不放宽
                    context={CONTEXT_APPROVED: {"image_host.upload"}},
                )
                self.referenceUploaded.emit(path, str(url))
                self._notify("info", "已上传，可直接用于视频参考图")
            except AppError as exc:
                self._notify("error", exc.user_message)

        self._runner.submit(_run())

    @Slot()
    def cancelUpload(self) -> None:
        """用户取消上传（`pending_approval` 的另一个出口）。"""
        self._pending_upload = ""
        self._view = replace(self._view, pending_approval="")
        self.stateChanged.emit()
        self._notify("info", "已取消上传")

    @Slot(str)
    def copyText(self, text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(text)
            self._notify("info", "已复制")

    @Slot(str)
    def openUrl(self, url: str) -> None:
        if url:
            webbrowser.open(url)

    @Slot(str)
    def openPath(self, path: str) -> None:
        """用系统默认程序打开本地文件（旧版「打开」/「播放」的做法）。"""
        if not path:
            return
        try:
            import os

            os.startfile(path)          # noqa: S606 - Windows 专用，失败会抛异常
        except Exception as exc:        # pragma: no cover - 依赖系统行为
            self._notify("error", f"打开失败：{exc}")

    @Slot(str, str, str)
    def saveAs(self, target: str, url: str, local: str) -> None:   # noqa: N802 - QML 调用
        """把产物另存到用户选定的位置（旧版结果区那个「保存」按钮的功能）。

        `target` 是界面选好的保存路径。取内容的顺序与旧版一致：
        **本地缓存优先**（快、离线也能存），没有才下载远端。

        下载走临时文件再原子替换：直接往目标路径写半截文件（网断了）会留下一个
        看着像成品、实际打不开的文件——那比失败更糟。
        """
        dest = str(target or "").strip()
        if not dest:
            return
        src = str(local or "").strip()
        remote = str(url or "").strip()
        if not src and not remote:
            self._notify("warn", "这条结果没有可保存的内容")
            return

        async def _run() -> None:
            import shutil
            from pathlib import Path

            out = Path(dest)
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                cached = Path(src) if src else None
                if cached is not None and cached.is_file():
                    shutil.copyfile(cached, out)          # 本地缓存：直接复制
                else:
                    if not remote:
                        self._notify("warn", "这条结果没有可下载的地址，也没留本地缓存")
                        return
                    data = await self._ctx.http.download(remote, timeout=180)
                    if not data:
                        self._notify("error", "下载到的是空内容，没有保存")
                        return
                    tmp = out.with_suffix(out.suffix + ".part")
                    tmp.write_bytes(data)
                    tmp.replace(out)                      # 原子替换：不会留下半截文件
                self._notify("info", f"已保存：{out}")
            except Exception as exc:
                log_error("ui_bridge.saveAs", exc)
                self._notify("error", f"保存失败：{type(exc).__name__}：{exc}")

        self._runner.submit(_run())

    @Slot(str, result=str)
    def suggestedFileName(self, kind: str) -> str:      # noqa: N802 - QML 调用
        """给「另存为」对话框用的默认文件名（沿用当前结果的时间戳 id）。"""
        ext = ".mp4" if str(kind) == "video" else ".png"
        stem = str(self._view.job_id or "").strip() or "result"
        return f"stillroom_{stem}{ext}"

    @Slot(str, result=str)
    def recordDetail(self, record_id: str) -> str:      # noqa: N802 - QML 调用
        """一条历史记录的完整详情文本（旧版那个「详情」弹层的内容）。

        为什么要有它：主界面刻意不铺全文，**排查问题的出口就只剩这里**。你这次遇到 503
        想事后回看「当时到底报了什么」，此前是查不到的——历史卡片只说「失败」。
        平台任务 ID（`meta.video_id`）也在这里：拿它才能找平台对账。
        """
        record = self._ctx.history.get(str(record_id))
        if record is None:
            return ""

        kind = "🎬 视频" if record.kind == "video" else "🖼 图片"
        status = {
            "success": "✅ 成功",
            "failed": "❌ 失败",
        }.get(str(record.status), str(record.status))
        stamp = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(record.created_at)
        ) if record.created_at else ""

        lines = [
            f"{kind} · {status} · {stamp}",
            f"记录 ID：{record.id}",
            "",
            "提示词：",
            "  " + (record.prompt or "（无）"),
            "",
            "输入参数：",
        ]
        params = record.params or {}
        param_lines = []
        for key, label in (("model", "模型"), ("size", "尺寸"),
                           ("seconds", "时长（秒）"), ("aspect_ratio", "画幅")):
            if params.get(key):
                param_lines.append(f"  {label}：{params[key]}")
        lines.append("\n".join(param_lines) if param_lines else "  （无）")

        meta = record.meta or {}
        # 平台任务 ID：视频出问题时靠它找平台对账（内部 job_id 对用户没用）
        remote_id = str(meta.get("video_id") or "").strip()
        if remote_id:
            lines.append(f"  平台任务 ID：{remote_id}")
        for key, label in (("size", "输出分辨率"), ("quality", "质量")):
            if record.kind == "video" and meta.get(key):
                lines.append(f"  {label}：{meta[key]}")

        refs = record.refs or []
        lines.append(f"  参考图：{len(refs)} 张" if refs else "  参考图：无")
        for index, ref in enumerate(refs, 1):
            shown = ref if len(ref) <= 72 else ref[:70] + "..."
            lines.append(f"    {index}. {shown}")

        if record.error:
            lines += ["", "失败原因：", "  " + str(record.error).replace("\n", "\n  ")]
        if record.result_url:
            lines += ["", f"结果地址：{record.result_url}"]
        if record.media_path:
            lines.append(f"本地缓存：{record.media_path}")
        if record.duration:
            lines.append(f"耗时：{record.duration:.1f} 秒")

        return "\n".join(lines)

    @Slot(int, result="QVariantList")
    def historyRecent(self, limit: int = 20) -> list:
        """最近的历史记录（Phase 4 的历史页会用同一份数据）。"""
        try:
            return [record.to_dict() for record in self._ctx.history.list(limit=int(limit))]
        except Exception as exc:        # pragma: no cover
            self._notify("error", f"读取历史失败：{exc}")
            return []

    @Slot(str)
    def loadParams(self, record_id: str) -> None:      # noqa: N802 - QML 调用
        """把一条历史记录的参数回填到生成页（旧版的「载入参数」）。"""
        record = self._ctx.history.get(record_id)
        if record is None:
            self._notify("warn", "这条记录已经不在了")
            return
        self._ctx.state.set("history", selected_id=record.id)
        self.paramsLoaded.emit(record.kind, dict(record.params or {}))
        self._notify("info", "参数已载入生成页")

    @Slot(str)
    def openRecordFile(self, record_id: str) -> None:   # noqa: N802
        """用系统程序打开某条记录的本地文件（没有本地文件时退回到远端地址）。"""
        record = self._ctx.history.get(record_id)
        if record is None:
            return
        if record.media_path:
            self.openPath(record.media_path)
        elif record.result_url:
            self.openUrl(record.result_url)

    # ---------------------------------------------------------------- 内部

    def _launch(self, tool: str, factory: Callable[[], JobHandle]) -> None:
        """在 asyncio 线程里启动任务（`create_task` 必须在循环线程调用）。

        **先在界面线程同步置为「排队中」**：状态回传是异步的，如果等回传再改，
        连点两下就会起两个任务。这个同步写入让 `busy` 立刻为真，是界面侧的并发闸门
        （服务层本身允许并发——未来的工作流要同时跑多个步骤）。
        """
        self._view = _View(tool=tool, status="pending", message="正在提交…")
        self._cancel_requested = False        # 新任务：清掉上一次的取消请求
        self.stateChanged.emit()

        async def _start() -> None:
            try:
                handle = factory()
            except Exception as exc:
                # 启动阶段炸了也要落地：否则界面会永远停在「排队中」，而应用没有控制台可看
                log_error("ui_bridge.start", exc)
                self._view = _View(
                    tool=tool,
                    status="failed",
                    error=f"{type(exc).__name__}：{exc}",
                    error_kind="internal",
                )
                self.stateChanged.emit()
                self._notify("error", f"任务没能启动（{type(exc).__name__}）：{exc}")
                return
            self._current = handle
            self._ctx.state.track(handle)
            self.jobStarted.emit(handle.id)
            if self._cancel_requested:
                handle.cancel()      # 取消请求早于句柄登记：补上

        self._runner.submit(_start())

    def _on_state_changed(self, section: str, payload: dict) -> None:
        """AppState 的通知回调（发生在 asyncio 线程）→ 转成 Qt 信号。"""
        if section != "generation":
            return
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        progress = payload.get("progress")
        self._view = _View(
            job_id=str(payload.get("job_id") or ""),
            tool=str(payload.get("tool") or ""),
            status=str(payload.get("status") or "idle"),
            progress=float(progress) if isinstance(progress, (int, float)) else -1.0,
            message=str(payload.get("message") or ""),
            result_url=str(result.get("url") or ""),
            result_local=str(result.get("media_path") or ""),
            error=str(payload.get("error") or ""),
            error_kind=str(payload.get("error_kind") or ""),
            # 生成任务的进度回调不该冲掉「等用户确认上传」这件事——
            # 上传与生成是两条独立的流程，状态各归各的。
            pending_approval=self._view.pending_approval,
        )
        self.stateChanged.emit()
        if self._view.status in ("succeeded", "failed", "canceled"):
            ok = self._view.status == "succeeded"
            self.jobFinished.emit(
                self._view.job_id, ok, self._view.error or self._view.message
            )

    def _notify(self, level: str, text: str) -> None:
        self.noticeRaised.emit(level, text)

    def _result_source(self) -> str:
        """预览用地址：本地缓存优先（离线可看），否则用远端 URL。"""
        if self._view.result_local:
            from pathlib import Path

            if Path(self._view.result_local).exists():
                return QUrl.fromLocalFile(self._view.result_local).toString()
        return self._view.result_url

    # ---------------------------------------------------------------- 收尾

    def detach(self) -> None:
        """断开与状态层的订阅（窗口关闭时调用）。"""
        try:
            self._unsubscribe_state()
        except Exception:               # pragma: no cover
            pass
