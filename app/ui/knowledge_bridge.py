"""知识库页面的后端：上传 / 构建 / 重建 / 删除 / 检索试跑。

与界面层其他部分一致：文件与库的操作都丢到 asyncio 线程里做（大文件哈希与解析慢），
界面只收结果。检索试跑走的是助手用的同一套 `kb.search`——这里看到的命中，就是助手会用的。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from app.bootstrap import AppContext
from app.config.logs import log_error
from app.services.knowledge import KnowledgeDocument, KnowledgeError
from app.ui.async_runner import AsyncRunner

_KIND_LABELS = {"txt": "文本", "md": "Markdown", "csv": "CSV", "json": "JSON", "pdf": "PDF"}
_STATE_LABELS = {"pending": "待构建", "ready": "可用", "failed": "构建失败"}


def _size_text(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _time_text(value: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value)) if value else ""


class KnowledgeBridge(QObject):
    """QML 上下文对象 `knowledgeBridge`。"""

    stateChanged = Signal()
    documentsChanged = Signal()
    resultsChanged = Signal()
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
        self._status = ""
        self._results: list[dict] = []
        self._reason = ""

    # ---------------------------------------------------------------- 属性

    busy = Property(bool, lambda self: self._busy, notify=stateChanged)
    statusText = Property(str, lambda self: self._status, notify=stateChanged)

    @Property("QVariantList", notify=documentsChanged)
    def documents(self) -> list[dict]:            # noqa: N802 - QML 命名
        return [self._row(doc) for doc in self._ctx.knowledge.documents()]

    @Property("QVariantList", notify=resultsChanged)
    def results(self) -> list[dict]:
        return list(self._results)

    resultReason = Property(str, lambda self: self._reason, notify=resultsChanged)  # noqa: N802

    @Slot(str, result=str)
    def documentPath(self, doc_id: str) -> str:    # noqa: N802
        """这份文件在知识库目录里的实际路径（界面上删文件时提示用）。"""
        doc = self._ctx.knowledge.get(str(doc_id))
        return doc.path if doc else ""

    @Slot(str, result=str)
    def localPath(self, url: str) -> str:          # noqa: N802
        """把文件框给的 `file://` URL 转成本地路径（Windows 盘符也认）。"""
        text = str(url or "")
        if text.startswith("file:"):
            local = QUrl(text).toLocalFile()
            return str(Path(local)) if local else ""
        return text

    # ---------------------------------------------------------------- 动作

    @Slot()
    def refresh(self) -> None:
        self.documentsChanged.emit()

    @Slot("QVariantList")
    def addFiles(self, paths) -> None:             # noqa: N802
        """导入文件：复制进知识库目录并**立刻构建**（上传就是为了能搜到）。"""
        files = [str(path) for path in (paths or []) if str(path or "").strip()]
        if not files:
            return
        self._begin(f"正在导入 {len(files)} 个文件……")

        async def _go() -> None:
            added = 0
            for path in files:
                name = Path(path).name
                try:
                    doc = await asyncio.to_thread(self._ctx.knowledge.add, path)
                    await asyncio.to_thread(self._ctx.knowledge.build, doc.id)
                except KnowledgeError as exc:
                    self._notify("warn", f"{name}：{exc}")
                    continue
                except Exception as exc:            # pragma: no cover - 意外错误也要看得见
                    log_error("knowledge_bridge.add", exc)
                    self._notify("error", f"{name} 导入失败：{type(exc).__name__}")
                    continue
                added += 1
            self._end(f"已导入并构建 {added} 个文件" if added else "没有文件导入成功")

        self._submit(_go())

    @Slot(str)
    def build(self, doc_id: str) -> None:          # noqa: N802
        """构建 / 重建一份文档（失败也留原因，见 `KnowledgeStore.build`）。"""
        doc = self._ctx.knowledge.get(str(doc_id))
        if doc is None:
            return
        self._begin(f"正在构建《{doc.name}》……")

        async def _go() -> None:
            try:
                built = await asyncio.to_thread(self._ctx.knowledge.build, doc.id)
            except Exception as exc:                # pragma: no cover
                log_error("knowledge_bridge.build", exc)
                self._end(f"构建失败：{type(exc).__name__}")
                return
            if built is not None and built.state == "failed":
                self._notify("warn", f"《{built.name}》没能构建：{built.error}")
                self._end("构建失败")
                return
            self._end(f"《{doc.name}》已构建：{built.chunk_count if built else 0} 片")

        self._submit(_go())

    @Slot(str, bool)
    def remove(self, doc_id: str, delete_file: bool) -> None:   # noqa: N802
        """删掉一份文档；`delete_file` 由界面上那个「连原文件一起删」决定。"""
        doc = self._ctx.knowledge.get(str(doc_id))
        self._begin("正在删除……")

        async def _go() -> None:
            # 分两步、由这里决定顺序：先删记录（库里的东西一定删掉），再删原文件。
            # 原文件删不掉时（Windows 上偶尔被索引/杀毒占用）要说清「记录删了、文件还在」，
            # 不能让用户以为磁盘已经干净了。
            removed = await asyncio.to_thread(
                self._ctx.knowledge.remove, str(doc_id), delete_file=False
            )
            name = doc.name if doc else "这份文件"
            if not removed:
                self._end("这份文件已经不在了")
                return
            error = ""
            if delete_file and doc is not None:
                error = await asyncio.to_thread(self._ctx.knowledge.delete_file, doc.path)
            if error:
                self._notify("warn", f"《{name}》的记录已删除，但原文件没删掉（{error}）：{doc.path}")
                self._end(f"已删除《{name}》（原文件仍留在磁盘上）")
            else:
                self._end(f"已删除《{name}》")

        self._submit(_go())

    @Slot(str)
    def testSearch(self, text: str) -> None:       # noqa: N802
        """检索试跑：只查知识库，看到的就是助手在「找参考」时会拿到的片段。"""
        query = str(text or "").strip()
        if not query:
            self._notify("warn", "先写一句话再试检索")
            return
        self._begin("正在检索知识库……")
        self._results = []
        self._reason = ""
        self.resultsChanged.emit()

        async def _go() -> None:
            try:
                found = await self._ctx.knowledge.search(query, top_k=5)
            except Exception as exc:                # pragma: no cover
                log_error("knowledge_bridge.search", exc)
                self._results, self._reason = [], f"检索出错：{type(exc).__name__}"
                self._end("检索失败")
                return
            self._results = [
                {
                    "name": hit.doc_name,
                    "ordinal": hit.ordinal,
                    "text": hit.text,
                    "snippet": hit.text[:160],
                    "sourceLabel": f"{hit.doc_name} · 第 {hit.ordinal + 1} 片",
                    "score": round(float(hit.score or 0.0), 2),
                }
                for hit in found.hits
            ]
            self._reason = found.reason
            self._end(f"命中 {len(self._results)} 片" + ("" if found.reranked else "（未重排）"))

        self._submit(_go())

    # ---------------------------------------------------------------- 内部

    def _row(self, doc: KnowledgeDocument) -> dict:
        return {
            "id": doc.id,
            "name": doc.name,
            "kind": doc.kind,
            "kindLabel": _KIND_LABELS.get(doc.kind, doc.kind),
            "sizeText": _size_text(doc.size),
            "state": doc.state,
            "stateLabel": _STATE_LABELS.get(doc.state, doc.state),
            "error": doc.error,
            "chunkCount": doc.chunk_count,
            "createdText": _time_text(doc.created_at),
            "builtText": _time_text(doc.built_at),
            "ready": doc.state == "ready",
            "failed": doc.state == "failed",
        }

    def _begin(self, status: str) -> None:
        self._busy = True
        self._status = status
        self.stateChanged.emit()

    def _end(self, status: str) -> None:
        self._busy = False
        self._status = status
        self.stateChanged.emit()
        self.documentsChanged.emit()
        self.resultsChanged.emit()

    def _submit(self, coro) -> None:
        self._runner.submit(coro)

    def _notify(self, level: str, text: str) -> None:
        self.noticeRaised.emit(level, text)
