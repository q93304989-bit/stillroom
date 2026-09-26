"""助手页的后端：把六步流水线暴露成一个「跑一次、看着它跑、能打断、会请示」的对象。

与 `UiBridge` 的分工：那边管手动生成（图片 / 视频 / 历史），这边只管一句话跑完整流程。
两者共用同一条 asyncio 线程与同一个 `AppContext`，所以闸门、预算、历史库都是同一份——
助手跑出来的图，和历史页里的记录是同一种记录。

线程约定与 `UiBridge` 一致：槽在 Qt 主线程被调用，真正的活在 asyncio 线程里干，
事件回调也发生在 asyncio 线程 → 在这里 emit Qt 信号（Qt 会排队回主线程）。

界面上「请示」一共四种，各有各的按钮，绝不混在一起：

    needs_input        理解阶段信息不够   → 补一句，重跑
    needs_confirmation 评估不确定/看不了图 → 「可用就采纳」或「不满意再改一版」
    needs_approval     工具要人工批准     → 「允许并重跑」（只放行这一个工具）
    budget_exceeded    额度用尽           → 「重新运行」（新的一次运行，预算重新计算）

四种都如实说明「这是新的一次运行」，而不是假装原运行还在继续。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from app.bootstrap import AppContext
from app.config.logs import log_error
from app.config import settings
from app.services.context_store import (
    DEFAULT_SOURCES,
    ContextDraft,
    ContextItem,
    ContextProfile,
)
from app.state.events import Event
from app.state.jobs import Job, JobHandle, JobStatus
from app.ui.async_runner import AsyncRunner
from app.ui.bridge import UiBridge


@dataclass
class _AgentView:
    """助手页要展示的一份快照（界面不自己拼状态，只读这里）。"""

    run_id: str = ""
    status: str = "idle"
    requirement: str = ""
    kind: str = "image"
    prompt: str = ""
    references: list[str] = field(default_factory=list)
    result_url: str = ""
    result_local: str = ""
    message: str = ""
    description: dict = field(default_factory=dict)
    evaluation: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    attempts: int = 0


_STATUS_TEXT = {
    "idle": "空闲",
    "running": "运行中",
    "succeeded": "已完成",
    "accepted": "已采纳",
    "needs_input": "需要你补充一句",
    "out_of_scope": "这活儿不在我的能力范围",
    "drafting": "正在找参考…",
    "draft": "上下文待你确认",
    "needs_confirmation": "需要你看一眼",
    "needs_approval": "需要你批准",
    "budget_exceeded": "额度用尽",
    "failed": "失败",
    "canceled": "已打断",
}

#: 一次运行的进度按「已开始的阶段数 / 正常路径的阶段数」估，用于进度条（不是精确值）
_PHASES_IN_HAPPY_PATH = 5

#: 上下文条目的类型标签（界面给用户看的词）
_ITEM_LABELS = {
    "history": "历史参考图",
    "history_text": "历史提示词片段",
    "kb": "知识库片段",
    "web": "联网线索",
    "web_image": "联网配图",
    "manual": "我加的",
}

#: 来源开关的名字（与 DEFAULT_SOURCES 的键对应）
_SOURCE_LABELS = {
    "history": "历史参考",
    "knowledge": "知识库",
    "web": "联网线索",
    "web_images": "联网配图",
}

#: 调用统计里的中文名（给用户看的账目）
_USAGE_LABELS = {
    "image.generate": "出图",
    "video.submit": "提交视频",
    "vision.describe": "看图",
    "judge.ask": "判断",
    "llm.chat": "写提示词",
    "rag.search": "找参考",
    "image_host.upload": "上传",
}


class AgentBridge(QObject):
    """QML 侧入口对象（注册为上下文属性 `agentBridge`）。"""

    stateChanged = Signal()
    noticeRaised = Signal(str, str)                 # level(info/warn/error), text
    stepsCleared = Signal()                         # 清空时间线（每次开跑前）
    stepAdded = Signal(str, str, str, bool, float)  # phase, title, detail, ok, seconds（实时）
    stepsReplaced = Signal("QVariantList")          # 运行结束后用带耗时的完整步骤替换
    runStarted = Signal(str)                        # run_id
    runFinished = Signal(str, str)                  # run_id, status
    contextChanged = Signal()                       # 上下文草稿变了（增删条目 / 改开关 / 改备注）
    profilesChanged = Signal()                      # 长期档案变了（存 / 删 / 设默认）

    def __init__(
        self,
        context: AppContext,
        runner: AsyncRunner,
        ui: UiBridge,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._ctx = context
        self._runner = runner
        self._ui = ui                              # 只借用它的「复制 / 打开 / 载入生成页」
        self._view = _AgentView()
        self._handle: JobHandle | None = None
        #: 打断请求早于句柄登记时的暂存（见 `cancel`）
        self._cancel_requested = False
        self._run_id = ""
        self._raw_steps: list[dict] = []
        self._live_steps = 0
        #: 当前上下文草稿：`_draft_id` 指向库里那份（用户改的就是它）。
        #: 类型 / 画幅这些用户不改的东西也存在草稿里，所以「跑完改参考再跑一次」能拿到。
        self._draft_id: str | None = None
        self._mode: str = str(settings.get("context_mode", "draft") or "draft")
        self._unsubscribe = context.bus.subscribe(self._on_event)

    # ---------------------------------------------------------------- 属性

    status = Property(str, lambda self: self._view.status, notify=stateChanged)
    statusText = Property(
        str, lambda self: _STATUS_TEXT.get(self._view.status, self._view.status), notify=stateChanged
    )
    running = Property(bool, lambda self: self._view.status == "running", notify=stateChanged)
    requirement = Property(str, lambda self: self._view.requirement, notify=stateChanged)
    kind = Property(str, lambda self: self._view.kind, notify=stateChanged)
    prompt = Property(str, lambda self: self._view.prompt, notify=stateChanged)
    message = Property(str, lambda self: self._view.message, notify=stateChanged)
    runId = Property(str, lambda self: self._view.run_id, notify=stateChanged)
    attempts = Property(int, lambda self: self._view.attempts, notify=stateChanged)
    resultUrl = Property(str, lambda self: self._view.result_url, notify=stateChanged)
    resultLocalPath = Property(str, lambda self: self._view.result_local, notify=stateChanged)
    hasResult = Property(
        bool,
        lambda self: bool(self._view.result_url or self._view.result_local),
        notify=stateChanged,
    )
    resultSource = Property(str, lambda self: self._result_source(), notify=stateChanged)
    referenceCount = Property(int, lambda self: len(self._view.references), notify=stateChanged)
    stepCount = Property(int, lambda self: self._live_steps, notify=stateChanged)

    @Property(float, notify=stateChanged)
    def progress(self) -> float:
        """粗略进度：已开始的阶段数 / 正常路径阶段数（只在跑的时候有意义）。"""
        if self._view.status == "succeeded":
            return 100.0
        if self._view.status == "running":
            return min(95.0, self._live_steps / _PHASES_IN_HAPPY_PATH * 100.0)
        return -1.0

    pendingAction = Property(str, lambda self: self._pending_action(), notify=stateChanged)
    needsUser = Property(bool, lambda self: self._pending_action() != "", notify=stateChanged)
    usageText = Property(str, lambda self: self._usage_text(), notify=stateChanged)
    decisionText = Property(str, lambda self: self._decision_text(), notify=stateChanged)

    # ---------------------------------------------------------------- 上下文（先看后跑）

    contextMode = Property(str, lambda self: self._mode, notify=stateChanged)
    hasContext = Property(bool, lambda self: self._draft_view() is not None, notify=contextChanged)
    contextEditable = Property(
        bool, lambda self: self._view.status == "draft", notify=stateChanged
    )
    contextRequirement = Property(
        str, lambda self: self._draft_text("requirement"), notify=contextChanged
    )
    contextNotes = Property(str, lambda self: self._draft_text("notes"), notify=contextChanged)
    contextSources = Property("QVariantMap", lambda self: self._draft_sources(), notify=contextChanged)
    contextItems = Property("QVariantList", lambda self: self._draft_items(), notify=contextChanged)
    contextRemoved = Property(int, lambda self: self._draft_removed_count(), notify=contextChanged)
    contextReason = Property(str, lambda self: self._draft_reason(), notify=contextChanged)
    #: 跑完能不能「改参考再跑一次」：有这次运行的快照就行（草稿模式与自动模式都有）
    canEditContext = Property(bool, lambda self: self._edit_source_run() != "", notify=stateChanged)
    #: 长期档案（用户攒下来的偏好，可一键套到当前草稿上）
    profiles = Property("QVariantList", lambda self: self._profile_rows(), notify=profilesChanged)
    hasProfiles = Property(bool, lambda self: bool(self._profile_rows()), notify=profilesChanged)

    # ---------------------------------------------------------------- 运行

    @Slot(str)
    def run(self, request: str) -> None:
        """一句话跑完整流程。

        草稿模式（默认）：先只跑「理解 + 找参考」，把结果摊成可编辑的上下文，等你确认；
        自动模式：直接跑（跑完仍可在结果卡上「改参考再跑一次」）。
        """
        if self._mode == "auto":
            self._launch(request)
        else:
            self._launch_draft(request)

    @Slot(str)
    def setContextMode(self, mode: str) -> None:
        """草稿模式 / 自动模式切换（记住到设置里）。"""
        value = "auto" if str(mode) == "auto" else "draft"
        if value == self._mode:
            return
        self._mode = value
        settings.update(context_mode=value)
        self._notify("info", "已切到" + ("自动模式：点「开始」直接跑" if value == "auto" else "草稿模式：先出上下文再跑"))
        self.stateChanged.emit()

    @Slot()
    def confirmContext(self) -> None:
        """按这份（可能改过的）上下文生成。"""
        payload = self._confirmed_payload()
        if payload is None:
            self._notify("warn", "现在已经没有待确认的上下文了")
            return
        self._notify("info", "按你确认的上下文开始生成")
        self._launch(
            str(payload["requirement"]),
            context=payload,
            draft_id=self._draft_id,
        )

    @Slot()
    def redraft(self) -> None:
        """重找一次：按当前开关重新检索，并保留你的裁决（删过的不复活）。"""
        draft = self._draft_view()
        if draft is None:
            return
        self._launch_draft(draft.requirement, existing=draft)

    @Slot()
    def discardContext(self) -> None:
        """丢掉这份草稿（不留运行记录）。"""
        if self._draft_id:
            self._ctx.contexts.finish(self._draft_id, state="dropped")
        self._draft_id = None
        self._view = _AgentView()
        self.stepsCleared.emit()
        self.stateChanged.emit()
        self.contextChanged.emit()

    @Slot(str)
    def removeContextItem(self, key: str) -> None:
        if not self._draft_id:
            return
        self._ctx.contexts.remove_item(self._draft_id, str(key))
        self._notify("info", "已删掉这条参考，本次不会再用它")
        self.contextChanged.emit()

    @Slot(str)
    def setContextNotes(self, text: str) -> None:
        if not self._draft_id:
            return
        self._ctx.contexts.update(self._draft_id, notes=str(text or ""))
        self.contextChanged.emit()

    @Slot(str)
    def setContextRequirement(self, text: str) -> None:
        if not self._draft_id:
            return
        self._ctx.contexts.update(self._draft_id, requirement=str(text or ""))
        self.contextChanged.emit()

    @Slot(str, str)
    def addContextText(self, text: str, title: str = "") -> None:
        """自己加一条文字要求（进提示词）。"""
        value = str(text or "").strip()
        if not self._draft_id or not value:
            return
        self._ctx.contexts.add_item(
            self._draft_id,
            ContextItem(kind="manual", ref=value, title=title or value[:40], origin="你加的"),
        )
        self.contextChanged.emit()

    @Slot(str, bool)
    def toggleContextSource(self, name: str, on: bool) -> None:
        """开关一个来源。关掉的来源在下一次「重找一次」里根本不会被检索。"""
        if not self._draft_id or name not in DEFAULT_SOURCES:
            return
        draft = self._draft_view()
        if draft is None:
            return
        sources = {**draft.sources, str(name): bool(on)}
        self._ctx.contexts.update(self._draft_id, sources=sources)
        self.contextChanged.emit()
        self._notify("info", ("打开" if on else "关闭") + f"了「{_SOURCE_LABELS.get(name, name)}」，下次重找生效")

    @Slot()
    def rememberContextSources(self) -> None:      # noqa: N802
        """把这份草稿上的来源开关记成默认值（设置页那三个开关也是这个值）。

        与「本次临时改一改」分开：改开关只影响这一份草稿，只有按了这里才会写进
        settings.json，下次新草稿照着来。
        """
        draft = self._draft_view()
        if draft is None:
            self._notify("warn", "现在没有可记住的上下文")
            return
        settings.update(context_sources={**DEFAULT_SOURCES, **dict(draft.sources)})
        self._notify("info", "已记住这份来源设置，下次新草稿按它来")

    @Slot()
    def cancel(self) -> None:
        """打断：判定与取消都回到 asyncio 线程里做。

        理由与 `UiBridge.cancel` 相同——句柄是异步登记的，界面线程直接判定「没有在跑的
        运行」会把刚点下的打断吞掉（慢一点的机器上必现）。
        """
        if not self.running:
            return
        self._notify("info", "已请求打断")
        self._runner.call(self._cancel_in_loop)

    def _cancel_in_loop(self) -> None:
        """在 asyncio 线程里判定并打断（判定顺序与句柄登记顺序一致）。"""
        handle = self._handle
        if handle is not None and not handle.done:
            handle.cancel()
        else:
            self._cancel_requested = True

    @Slot()
    def clear(self) -> None:
        """清空助手页（正在跑的时候不允许，避免把运行中的界面清成空白）。"""
        if self.running:
            self._notify("warn", "还在跑，先「打断」再清空")
            return
        self._view = _AgentView()
        self._raw_steps = []
        self._live_steps = 0
        self.stepsCleared.emit()
        self.stateChanged.emit()

    # ---------------------------------------------------------------- 四种请示

    @Slot(str)
    def continueWith(self, supplement: str) -> None:
        """补充后重跑（`needs_input`）或换个方向再出一版（`needs_confirmation`）。"""
        status = self._view.status
        if status not in ("needs_input", "out_of_scope", "needs_confirmation"):
            return
        extra = (supplement or "").strip()
        if status == "needs_input" and not extra:
            self._notify("warn", "先补一句：主体、风格或用途")
            return
        hint = extra or "上一版不满意，请换个明显不同的方向或风格"
        self._notify("info", "已按补充内容重新起一次运行")
        self._launch(f"{self._view.requirement}。{hint}")

    @Slot()
    def runAnyway(self) -> None:
        """不补了，直接开跑（`needs_input` 时的出口）。

        判断模型偶尔就是觉得「不够开工」，而用户自己清楚够用了。这里只跳过
        「信息够不够」这一道追问，预算 / 白名单 / 审批照旧。
        """
        if self._view.status not in ("needs_input", "out_of_scope"):
            return
        self._notify("info", "按原话直接开跑")
        self._launch(self._view.requirement, force=True)

    @Slot()
    def acceptResult(self) -> None:
        """采纳这张（`needs_confirmation`）：写进历史信号，下次找参考会优先用它。"""
        if self._view.status != "needs_confirmation":
            return
        record = self._record_of_run()
        if record is None:
            self._notify("warn", "没找到这次的记录，可能已被清理")
            return
        self._ctx.history.set_feedback(record.id, action="accept")
        self._view.status = "accepted"
        self._view.message = "已采纳。这条记录标了「采纳」，以后找参考会优先带上它"
        self.stateChanged.emit()
        self._notify("info", "已采纳")

    @Slot()
    def approveAndRetry(self) -> None:            # noqa: N802 - QML 调用
        """批准一个工具后重跑（`needs_approval`）：只放行这一个工具，别的不放宽。"""
        tool = str(self._view.pending.get("tool") or "")
        if not tool:
            self._notify("warn", "没有待批准的动作")
            return
        self._notify("info", f"已批准 {tool}，正在重跑")
        self._launch(self._view.requirement, approved=(tool,))

    @Slot()
    def rerun(self) -> None:
        """原样再跑一次（额度用尽、失败、或不满意时用）。"""
        if not self._view.requirement:
            return
        self._notify("info", "重新起了一次运行（预算重新计算）")
        self._launch(self._view.requirement)

    @Slot()
    def editContext(self) -> None:                # noqa: N802 - QML 调用
        """跑完「改参考再跑一次」：把这次运行实际用的上下文**复制**成一份可编辑草稿。

        复制而不是复用原行：原快照是那次运行的留痕，事后不该被改写（方案里的硬规则）。
        复制出来的草稿与原草稿一样可增删、可改需求、可改来源开关，确认后再跑一次。
        """
        run_id = self._edit_source_run()
        if not run_id:
            self._notify("warn", "这次运行没有可改的上下文快照")
            return
        draft = self._ctx.contexts.reopen(run_id)
        if draft is None:                          # pragma: no cover - 只有库坏了才会走到
            self._notify("warn", "没能把这次的上下文取出来")
            return
        self._draft_id = draft.id
        self._view = replace(
            self._view,
            status="draft",
            message="下面是你上次实际用的上下文，改完再生成",
        )
        self.stateChanged.emit()
        self.contextChanged.emit()
        self._notify("info", "已把这次的参考摊开，改完点「按这份上下文生成」")

    @Slot(str, str, bool)
    def saveProfile(self, name: str, notes: str = "", as_default: bool = False) -> None:  # noqa: N802
        """把当前草稿的条目与备注存成一份长期档案（下次新草稿可一键套用）。"""
        draft = self._draft_view()
        if draft is None:
            self._notify("warn", "先把上下文摊开（草稿模式点「开始」），才能存成档案")
            return
        profile = self._ctx.contexts.save_profile(
            str(name or "").strip() or draft.requirement[:24],
            draft.kept,
            notes=str(notes or draft.notes or ""),
            is_default=bool(as_default),
        )
        self.profilesChanged.emit()
        self._notify("info", f"已存成档案「{profile.name}」"
                             + ("，并设为默认" if profile.is_default else ""))

    @Slot(str)
    def applyProfile(self, profile_id: str) -> None:   # noqa: N802
        """把一份档案套到当前草稿上（条目并进来，用户删过的照旧不复活）。"""
        draft = self._draft_view()
        if draft is None:
            self._notify("warn", "现在没有可以套档案的上下文")
            return
        profile = self._ctx.contexts.get_profile(str(profile_id))
        if profile is None:
            self._notify("warn", "这份档案已经不在了")
            return
        self._ctx.contexts.apply_profile(profile, draft.id)
        self.contextChanged.emit()
        self._notify("info", f"已套用档案「{profile.name}」")

    @Slot(str)
    def deleteProfile(self, profile_id: str) -> None:  # noqa: N802
        if self._ctx.contexts.delete_profile(str(profile_id)):
            self.profilesChanged.emit()
            self._notify("info", "已删掉这份档案（已有的运行不受影响）")

    @Slot(str)
    def setDefaultProfile(self, profile_id: str) -> None:  # noqa: N802
        """设成默认档案。默认档案在每次新草稿时自动套上（不用手动点「套用」）。"""
        profile = self._ctx.contexts.set_default_profile(str(profile_id))
        self.profilesChanged.emit()
        if profile is not None:
            self._notify("info", f"「{profile.name}」已设为默认，新草稿会自动带上它")

    # ---------------------------------------------------------------- 结果操作

    @Slot()
    def openResult(self) -> None:                 # noqa: N802
        """打开结果：本地文件优先（离线也能看），否则打开远端地址。"""
        if self._view.result_local and Path(self._view.result_local).exists():
            self._ui.openPath(self._view.result_local)
        elif self._view.result_url:
            self._ui.openUrl(self._view.result_url)

    @Slot()
    def loadIntoGenerator(self) -> None:          # noqa: N802
        """把这次用的提示词与参考图填进生成页（想手动接着调的时候用）。"""
        if not self._view.prompt:
            self._notify("warn", "还没有可载入的提示词")
            return
        refs = list(self._view.references)
        if self._view.kind == "video":
            # 视频接口只吃公网 URL，本地路径填进去只会白等一次 400
            refs = [ref for ref in refs if str(ref).startswith("http")]
        params = {
            "prompt": self._view.prompt,
            "images": refs,
        }
        # 复用生成页已有的「载入参数」通道；Main.qml 收到后会自己切到对应页面
        self._ui.paramsLoaded.emit(self._view.kind, params)
        self._notify("info", "提示词与参考图已载入生成页")

    @Slot(result="QVariantList")
    def currentSteps(self) -> list:               # noqa: N802
        """最近一次运行的完整步骤（界面重建时间线时用；没跑过就是空列表）。"""
        return [dict(step) for step in self._raw_steps]

    # ---------------------------------------------------------------- 内部

    def _launch(
        self,
        request: str,
        *,
        approved: tuple[str, ...] = (),
        force: bool = False,
        context: dict | None = None,
        draft_id: str | None = None,
    ) -> None:
        text = (request or "").strip()
        if not text:
            self._notify("warn", "先写一句你想要什么")
            return
        if self.running:
            self._notify("warn", "上一次还在跑，可以先「打断」")
            return

        # 同步置为「运行中」：状态回传是异步的，等回传再改的话，连点两下会起两次运行
        self._view = _AgentView(status="running", requirement=text)
        self._raw_steps = []
        self._live_steps = 0
        self._cancel_requested = False        # 新运行：清掉上一次的打断请求
        self.stepsCleared.emit()
        self.stateChanged.emit()

        async def _go() -> None:
            try:
                handle = self._ctx.agent.start(
                    text, approved=approved, force=force, context=context
                )
            except Exception as exc:              # 装配错误：也要让界面说清，别静默
                log_error("agent_bridge.start", exc)
                self._settle_error("", f"启动失败（{type(exc).__name__}）：{exc}")
                return
            self._handle = handle
            self._run_id = handle.id
            if draft_id:
                # 这份上下文就是那次运行的快照：出问题能回放「当时给它看了什么」
                self._ctx.contexts.bind_run(draft_id, handle.id)
            self.runStarted.emit(handle.id)
            if self._cancel_requested:
                # 打断请求早于句柄登记：现在补上。还没开始跑就被取消也没问题，
                # 下面的 `wait()` 会把取消消化成「已取消」的终态。
                handle.cancel()
            try:
                job = await handle.wait()
            except Exception as exc:
                # 运行里冒出的意外异常：界面必须给出一个「失败 + 原因」，不能停在上一次的样子
                log_error("agent_bridge.wait", exc)
                self._settle_error(
                    handle.id, f"运行出错（{type(exc).__name__}）：{exc}"
                )
                return
            if draft_id:
                self._ctx.contexts.finish(draft_id)
            elif job.result:
                # 自动模式没有草稿，但跑完仍要能「改参考再跑一次」——把这次实际用了什么
                # 落成一份绑定 run_id 的快照。失败 / 被取消的运行不落（没有可改的意义）。
                self._snapshot_auto_run(handle.id, job)
            self._settle(job)

        self._runner.submit(_go())

    def _snapshot_auto_run(self, run_id: str, job: Job) -> None:
        """自动模式跑完后留一份上下文快照（草稿模式已有快照，不重复落）。"""
        data = job.result if isinstance(job.result, dict) else {}
        payload = data.get("context") or {}
        if str(data.get("status") or "") != "succeeded" or not payload:
            return
        try:
            self._ctx.contexts.save_snapshot(
                run_id,
                str(payload.get("requirement") or ""),
                [ContextItem.from_dict(item) for item in (payload.get("items") or [])],
                notes=str(payload.get("notes") or ""),
                sources=payload.get("sources") or DEFAULT_SOURCES,
                decide=payload.get("decide") or {},
                kind=str(payload.get("kind") or "image"),
                aspect=str(payload.get("aspect") or "16:9"),
            )
        except Exception as exc:                  # 快照落不下来不该影响这次运行的结果
            log_error("agent_bridge.snapshot", exc)

    # ---------------------------------------------------------------- 草稿（先看后跑）

    def _launch_draft(self, request: str, *, existing: ContextDraft | None = None) -> None:
        """跑「理解 + 找参考」，把结果落成一份可编辑的上下文草稿。"""
        text = (request or "").strip()
        if not text:
            self._notify("warn", "先写一句你想要什么")
            return
        if self.running:
            self._notify("warn", "上一次还在跑，可以先「打断」")
            return

        if existing is not None:
            sources = dict(existing.sources)
        else:
            # 新草稿：来源默认值取自设置（用户上次「记住为默认」的结果）
            saved = settings.get("context_sources", {}) or {}
            sources = {**DEFAULT_SOURCES, **dict(saved)}
        self._view = _AgentView(status="drafting", requirement=text)
        self._raw_steps = []
        self._live_steps = 0
        self.stepsCleared.emit()
        self.stateChanged.emit()

        async def _go() -> None:
            try:
                payload = await self._ctx.agent.draft(text, sources=sources)
            except Exception as exc:
                log_error("agent_bridge.draft", exc)
                self._settle_error("", f"找参考出错（{type(exc).__name__}）：{exc}")
                return
            status = str(payload.get("status") or "ok")
            if status != "ok":
                # 与以前一致：信息不够 / 这活儿做不了，都是「停在第 1 步说清楚」
                self._view = _AgentView(
                    status=status,
                    requirement=str(payload.get("requirement") or text),
                    message=str(payload.get("message") or ""),
                    kind=str(payload.get("kind") or "image"),
                )
                self.stateChanged.emit()
                return
            self._store_draft(payload, existing=existing)

        self._runner.submit(_go())

    def _store_draft(self, payload: dict, *, existing: ContextDraft | None) -> None:
        """把草稿写进库（重找一次时沿用同一份，用户删掉的条目继续被过滤）。"""
        items = [ContextItem.from_dict(item) for item in (payload.get("items") or [])]
        # 出草稿时定下的类型与画幅落进库里那份草稿（以前存在 `_draft_meta` 里，
        # 于是「跑完改参考再跑一次」拿不到它——现在它是草稿自身的一部分）。
        kind = str(payload.get("kind") or "image")
        aspect = str(payload.get("aspect") or "16:9")
        if existing is not None:
            # 重找一次：新结果并进来，但用户删过的键不许复活
            from app.services.context_store import merge_items

            items = merge_items(items, removed_keys=existing.removed_keys, existing=existing.items)
            draft = self._ctx.contexts.update(
                existing.id,
                requirement=str(payload.get("requirement") or existing.requirement),
                items=items,
                sources=payload.get("sources") or existing.sources,
                decide=payload.get("decide") or {},
                kind=kind,
                aspect=aspect,
            )
            self._notify("info", "重找完成；你删掉的条目不会回来")
        else:
            draft = self._ctx.contexts.create(
                str(payload.get("requirement") or ""),
                items,
                sources=payload.get("sources") or DEFAULT_SOURCES,
                decide=payload.get("decide") or {},
                kind=kind,
                aspect=aspect,
            )
            # 草稿不该在库里堆着：新的来，旧的标 dropped
            self._ctx.contexts.drop_stale_drafts(keep_id=draft.id if draft else None)
        if draft is None:                          # pragma: no cover - 只有库坏了才会走到
            self._settle_error("", "草稿没能存下来")
            return
        self._draft_id = draft.id
        if existing is None:
            # 新草稿自动带上默认档案的偏好（「偏好能沉淀复用」的落点）
            profile = self._ctx.contexts.default_profile()
            if profile is not None:
                updated = self._ctx.contexts.apply_profile(profile, draft.id)
                if updated is not None:
                    draft = updated
        self._view = _AgentView(
            status="draft",
            requirement=draft.requirement,
            kind=draft.kind,
            message="",
        )
        self.stateChanged.emit()
        self.contextChanged.emit()

    def _draft_view(self) -> ContextDraft | None:
        if not self._draft_id:
            return None
        return self._ctx.contexts.get(self._draft_id)

    def _edit_source_run(self) -> str:
        """拿「改参考再跑一次」的底本：优先当前草稿绑的那次运行，其次是刚跑完的那次。"""
        draft = self._draft_view()
        if draft is not None and draft.run_id:
            return str(draft.run_id)
        run_id = str(self._view.run_id or "")
        if not run_id:
            return ""
        return run_id if self._ctx.contexts.of_run(run_id) is not None else ""

    def _profile_rows(self) -> list[dict]:
        return [
            {
                "id": profile.id,
                "name": profile.name,
                "itemCount": len(profile.items),
                "notes": profile.notes,
                "isDefault": bool(profile.is_default),
            }
            for profile in self._ctx.contexts.list_profiles()
        ]

    def _draft_text(self, field_name: str) -> str:
        draft = self._draft_view()
        return str(getattr(draft, field_name) or "") if draft else ""

    def _draft_sources(self) -> dict:
        draft = self._draft_view()
        return dict(draft.sources) if draft else dict(DEFAULT_SOURCES)

    def _draft_items(self) -> list[dict]:
        draft = self._draft_view()
        if draft is None:
            return []
        return [
            {
                "key": item.key,
                "kind": item.kind,
                "kindLabel": _ITEM_LABELS.get(item.kind, item.kind),
                "title": (item.title or item.ref)[:70],
                "origin": item.origin,
                "removed": item.user_state == "removed",
            }
            for item in draft.items
        ]

    def _draft_removed_count(self) -> int:
        draft = self._draft_view()
        if draft is None:
            return 0
        return sum(1 for item in draft.items if item.user_state == "removed")

    def _draft_reason(self) -> str:
        """草稿卡顶上那句说明：查了哪些来源、关了哪些（联网与否的理由将来也落在这里）。"""
        draft = self._draft_view()
        if draft is None:
            return ""
        decide = draft.decide or {}
        reason = str(decide.get("reason") or "")
        if reason:
            return reason
        labels = (
            ("history", "历史参考"),
            ("knowledge", "知识库"),
            ("web", "联网线索"),
            ("web_images", "联网配图"),
        )
        searched = [label for key, label in labels if decide.get(key) is True]
        closed = [label for key, label in labels if decide.get(key) is False]
        if not searched:
            return "来源都关着，这次没有找参考"
        text = "已查：" + "、".join(searched)
        if closed:
            text += "；已关：" + "、".join(closed)
        return text

    def _confirmed_payload(self) -> dict | None:
        """确认时给运行时的载荷：用户改过的那份草稿 + 出草稿时定下的 kind / aspect。"""
        draft = self._draft_view()
        if draft is None:
            return None
        return {
            "requirement": draft.requirement,
            "notes": draft.notes,
            "sources": dict(draft.sources),
            "items": [item.to_dict() for item in draft.items],
            "decide": dict(draft.decide),
            "kind": draft.kind,
            "aspect": draft.aspect,
        }

    def _settle(self, job: Job) -> None:
        """运行结束（发生在 asyncio 线程）：把结论写进视图，并用完整步骤替换时间线。

        注意先把结论放进局部变量、再用局部变量发信号：`self._view` 是界面线程也会读的
        共享字段，界面完全可能在信号半路发出时**已经开始下一次运行**并把它换成「运行中」。
        实测踩过这个坑：第二次运行刚起步，第一次运行的收尾信号报出的却是 "running"。
        """
        data = job.result if isinstance(job.result, dict) else {}
        if not data:
            if job.status is JobStatus.CANCELED:
                self._settle_error(job.id, "已打断", status="canceled")
            else:
                self._settle_error(job.id, job.error or "运行没有产出结果")
            return

        view = _AgentView(
            run_id=str(data.get("run_id") or job.id),
            status=str(data.get("status") or "failed"),
            requirement=str(data.get("requirement") or self._view.requirement),
            kind=str(data.get("kind") or "image"),
            prompt=str(data.get("prompt") or ""),
            references=[str(item) for item in (data.get("references") or [])],
            result_url=str(data.get("result_url") or ""),
            result_local=str(data.get("local_path") or ""),
            message=str(data.get("message") or ""),
            description=dict(data.get("description") or {}),
            evaluation=dict(data.get("evaluation") or {}),
            usage=dict(data.get("usage") or {}),
            pending=dict(data.get("pending") or {}),
            attempts=int(data.get("attempts") or 0),
        )
        steps = [dict(step) for step in (data.get("steps") or [])]

        self._view = view
        self._raw_steps = steps
        self._live_steps = len(steps)
        self.stepsReplaced.emit(list(steps))       # 实时那几条只有标题，这里是带耗时的完整版
        self.stateChanged.emit()
        self.runFinished.emit(view.run_id, view.status)

    def _settle_error(self, run_id: str, message: str, *, status: str = "failed") -> None:
        view = self._view
        if run_id and view.run_id and view.run_id != run_id:
            return                                    # 界面已经在跑下一次，别用它覆盖
        view = replace(view, status=status, message=message)
        self._view = view
        self.stateChanged.emit()
        self.runFinished.emit(run_id or view.run_id, status)
        if status == "failed":
            self._notify("error", message)

    def _on_event(self, event: Event) -> None:
        """事件总线的回调（发生在 asyncio 线程）；只认本次运行的 `agent.*` 事件。"""
        if not event.type.startswith("agent.") or event.job_id != self._run_id:
            return
        if event.type == "agent.phase":
            self._live_steps += 1
            self.stepAdded.emit(
                str(event.get("phase") or ""),
                str(event.get("title") or event.get("phase") or ""),
                "",
                True,
                0.0,
            )
            self.stateChanged.emit()
        elif event.type == "agent.decision":
            self.stepAdded.emit("decision", "这一轮的判断", _verdict_text(event.payload), True, 0.0)
            self.stateChanged.emit()

    # ---------------------------------------------------------------- 取值辅助

    def _pending_action(self) -> str:
        status = self._view.status
        if status in ("needs_input", "out_of_scope"):
            return "input"
        if status == "needs_confirmation":
            return "confirm"
        if status == "needs_approval":
            return "approval"
        if status == "budget_exceeded":
            return "budget"
        return ""

    def _usage_text(self) -> str:
        parts = [
            f"{_USAGE_LABELS.get(tool, tool)} {count} 次"
            for tool, count in (self._view.usage or {}).items()
        ]
        return " · ".join(parts)

    def _decision_text(self) -> str:
        verdict = self._view.evaluation or {}
        if not verdict:
            return ""
        return _verdict_text(verdict)

    def _result_source(self) -> str:
        """预览用地址：本地缓存优先（离线可看），否则用远端 URL。"""
        if self._view.result_local:
            if Path(self._view.result_local).exists():
                return QUrl.fromLocalFile(self._view.result_local).toString()
        return self._view.result_url

    def _record_of_run(self):
        """这次运行对应的历史记录（靠生成阶段记下的 job_id 找）。"""
        job_id = ""
        for step in self._raw_steps:
            if step.get("phase") == "generate":
                job_id = str((step.get("data") or {}).get("job_id") or "")
                if job_id:
                    break
        if not job_id:
            return None
        for record in self._ctx.history.list(limit=20):
            if record.job_id == job_id:
                return record
        return None

    def _notify(self, level: str, text: str) -> None:
        self.noticeRaised.emit(level, text)

    # ---------------------------------------------------------------- 收尾

    def detach(self) -> None:
        """断开事件订阅（窗口关闭时调用）。"""
        try:
            self._unsubscribe()
        except Exception:                         # pragma: no cover
            pass


def _verdict_text(verdict) -> str:
    """把一次判断压成一行中文（符合度 / 可用度 / 置信度 → 结论）。"""
    if not verdict:
        return ""
    decision = {
        "accept": "达标，直接交付",
        "refine": "还差一点，改提示词再来一轮",
        "confirm": "拿不准，交给你看",
    }.get(str(verdict.get("decision") or ""), str(verdict.get("decision") or ""))
    parts = [f"符合度={verdict.get('fits')}", f"可用度={verdict.get('quality')}"]
    if verdict.get("confidence") is not None:
        parts.append(f"置信度={verdict.get('confidence')}")
    return " ".join(str(part) for part in parts) + (f" → {decision}" if decision else "")
