"""设置页「自更新」卡片的后端：样本进度、生成建议、接受/拒绝、回滚。

与其他桥同一约定：槽在 Qt 主线程被调，LLM 调用在 asyncio 线程里做，
结果靠信号回来。建议本身由 `app/agent/self_update.py` 产生，
落地与版本管理由 `app/agent/prompt_store.py` 负责——这里只做「界面 ↔ 它们」的翻译。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Property, QObject, Signal, Slot

from app.agent import self_update
from app.agent.prompt_store import summarize_patch
from app.bootstrap import AppContext
from app.ui.async_runner import AsyncRunner


class SelfUpdateBridge(QObject):
    """QML 上下文对象 `selfUpdateBridge`。"""

    stateChanged = Signal()
    noticeRaised = Signal(str, str)                 # level(info/warn/error), text

    def __init__(
        self,
        context: AppContext,
        runner: AsyncRunner,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._ctx = context
        self._runner = runner
        self._busy = False

    # ---------------------------------------------------------------- 属性

    busy = Property(bool, lambda self: self._busy, notify=stateChanged)

    @Property(int, notify=stateChanged)
    def sampleCount(self) -> int:                 # noqa: N802 - QML 命名
        return self._safe(self_update.sample_count, 0)

    @Property(int, constant=True)
    def sampleNeeded(self) -> int:                # noqa: N802
        return self_update.MIN_SAMPLES

    @Property(int, notify=stateChanged)
    def currentVersion(self) -> int:              # noqa: N802
        return self._ctx.prompts.current_version()

    @Property(str, notify=stateChanged)
    def currentText(self) -> str:                 # noqa: N802
        row = self._ctx.prompts.current_row()
        if row is None:
            return "出厂行为（没有补丁）"
        return summarize_patch(row["patch"])

    @Property(str, notify=stateChanged)
    def currentReason(self) -> str:               # noqa: N802
        row = self._ctx.prompts.current_row()
        return str(row["reason"]) if row else ""

    hasPending = Property(
        bool, lambda self: self._ctx.prompts.pending() is not None, notify=stateChanged
    )

    @Property(str, notify=stateChanged)
    def pendingText(self) -> str:                 # noqa: N802
        row = self._ctx.prompts.pending()
        if row is None:
            return ""
        reason = str(row.get("reason") or "")
        body = summarize_patch(row["patch"])
        evidence = row.get("evidence") or {}
        samples = evidence.get("samples")
        prefix = f"依据最近 {samples} 次带信号的运行。\n" if samples else ""
        return prefix + (f"理由：{reason}\n" if reason else "") + body

    canRollback = Property(                       # noqa: N802
        bool, lambda self: self._ctx.prompts.can_rollback(), notify=stateChanged
    )

    # ---------------------------------------------------------------- 动作

    @Slot()
    def generateSuggestion(self) -> None:         # noqa: N802
        """分析最近的运行，让 LLM 给出一份补丁草案（只提议，不自动改）。"""
        if self._busy:
            return
        count = self.sampleCount
        if count < self_update.MIN_SAMPLES:
            self._notify(
                "warn",
                f"样本还不够：现在有 {count} 次带信号的运行，攒够 "
                f"{self_update.MIN_SAMPLES} 次才能出建议",
            )
            return
        self._busy = True
        self.stateChanged.emit()

        async def _go() -> None:
            try:
                outcome = await self_update.generate_suggestion(
                    self._ctx.registry, self._ctx.history
                )
            except Exception as exc:              # pragma: no cover - 装配错误才会走到
                outcome = {"ok": False, "reason": "error", "message": str(exc)}
            finally:
                self._busy = False

            if not outcome.get("ok"):
                reason = outcome.get("reason")
                if reason == "samples":
                    self._notify(
                        "warn",
                        f"样本还不够：还差 {outcome.get('short_by')} 次带信号的运行",
                    )
                elif reason == "llm":
                    self._notify("error", f"生成建议失败：{outcome.get('message')}")
                elif reason == "empty":
                    self._notify("info", "模型认为现在的提示词没有值得改的")
                else:
                    self._notify("warn", str(outcome.get("message") or "没能生成建议"))
            else:
                row = self._ctx.prompts.propose(
                    outcome["patch"],
                    reason=outcome.get("reason", ""),
                    evidence=outcome.get("evidence"),
                )
                if row is None:
                    self._notify("info", "模型给出的补丁是空的，没有可登记的建议")
                else:
                    self._notify("info", "建议已生成，请在设置页确认")
            self.stateChanged.emit()

        self._runner.submit(_go())

    @Slot()
    def acceptPending(self) -> None:              # noqa: N802
        """接受当前建议：立即生效（下一次运行就用新补丁），旧版本保留可回滚。"""
        row = self._ctx.prompts.pending()
        if row is None:
            return
        accepted = self._ctx.prompts.accept(row["id"])
        if accepted is not None:
            self._notify("info", f"已接受，当前版本 v{accepted['version']}，下一次运行生效")
        self.stateChanged.emit()

    @Slot()
    def rejectPending(self) -> None:              # noqa: N802
        """拒绝当前建议（留痕，以后翻记录能看到）。"""
        row = self._ctx.prompts.pending()
        if row is None:
            return
        self._ctx.prompts.reject(row["id"])
        self._notify("info", "已拒绝这条建议")
        self.stateChanged.emit()

    @Slot()
    def rollback(self) -> None:
        """回滚到上一个版本。"""
        row = self._ctx.prompts.rollback()
        if row is None:
            self._notify("warn", "没有可回滚的旧版本")
        else:
            self._notify("info", f"已回滚到 v{row['version']}")
        self.stateChanged.emit()

    @Slot(result="QVariantList")
    def versions(self) -> list:
        """版本历史（含被拒绝的，接受/拒绝都留痕），最新在前。"""
        rows = self._ctx.prompts.list(limit=20)
        out = []
        for row in rows:
            out.append({
                "version": row["version"] if row["version"] is not None else 0,
                "status": row["status"],
                "text": summarize_patch(row["patch"]),
                "reason": str(row.get("reason") or ""),
                "when": time.strftime("%m-%d %H:%M", time.localtime(row["created_at"])),
            })
        return out

    @Slot()
    def refresh(self) -> None:
        self.stateChanged.emit()

    # ---------------------------------------------------------------- 内部

    def _safe(self, fn, fallback):
        try:
            return fn(self._ctx.history)
        except Exception:
            return fallback

    def _notify(self, level: str, text: str) -> None:
        self.noticeRaised.emit(level, text)
