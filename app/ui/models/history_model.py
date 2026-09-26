"""历史记录列表模型（列表视图与画廊视图共用）。

三条与流畅度直接相关的设计：

1. **不在界面线程查库 / 不做解码**：查 SQLite 与生成缩略图都丢给 asyncio 线程里的
   `to_thread`，界面线程只接收结果。
2. **模型改动必须回界面线程**：Qt 要求 `beginResetModel` / `dataChanged` 发生在模型所属
   线程（界面线程）。这里用「跨线程信号 → 槽」回到界面线程，而不是在工作线程里直接改模型。
3. **缩略图按需补**：先出列表（可能没图），再后台一张张补上、只刷新对应行——首屏因此
   不必等全部缩略图就绪（对应「首屏可交互」这条预算）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    Property,
    Qt,
    QUrl,
    Signal,
    Slot,
)

from app.bootstrap import AppContext
from app.services.history import Record
from app.ui.async_runner import AsyncRunner

#: 一次最多预热多少张缩略图（避免一进页面就排几百个解码任务）
THUMB_PREFETCH_LIMIT = 120
#: 缩略图补齐的刷新节流：合并成一批再通知界面，避免滚动时被逐行刷新打断
THUMB_FLUSH_INTERVAL = 0.25


class HistoryModel(QAbstractListModel):
    IdRole = Qt.UserRole + 1
    KindRole = Qt.UserRole + 2
    StatusRole = Qt.UserRole + 3
    PromptRole = Qt.UserRole + 4
    TimeRole = Qt.UserRole + 5
    ThumbRole = Qt.UserRole + 6
    ResultUrlRole = Qt.UserRole + 7
    MediaPathRole = Qt.UserRole + 8
    BadgeRole = Qt.UserRole + 9
    DurationRole = Qt.UserRole + 10
    ErrorRole = Qt.UserRole + 11
    ParamsRole = Qt.UserRole + 12
    FavoriteRole = Qt.UserRole + 13
    TagsRole = Qt.UserRole + 14

    countChanged = Signal()
    loadingChanged = Signal()

    #: 工作线程 → 界面线程的通道（跨线程 emit 会被 Qt 排队到界面线程执行）
    _loaded = Signal(int, object, object)
    _rowsUpdated = Signal(int, object)

    def __init__(
        self,
        context: AppContext,
        runner: AsyncRunner,
        *,
        parent=None,
        prefetch_limit: int = THUMB_PREFETCH_LIMIT,
    ) -> None:
        super().__init__(parent)
        self._ctx = context
        self._runner = runner
        self._records: list[Record] = []
        self._thumbs: dict[str, str] = {}
        self._loading = False
        self._generation = 0
        self._filter = "all"
        self._keyword = ""
        self._prefetch_limit = prefetch_limit
        self._loaded.connect(self._apply_loaded)
        self._rowsUpdated.connect(self._apply_rows_update)

    # ---------------------------------------------------------------- 只读属性

    loading = Property(bool, lambda self: self._loading, notify=loadingChanged)
    count = Property(int, lambda self: len(self._records), notify=countChanged)

    @Slot(result=str)
    def currentFilter(self) -> str:
        return self._filter

    @Slot(result=str)
    def currentKeyword(self) -> str:
        return self._keyword

    # ---------------------------------------------------------------- 角色

    def roleNames(self) -> dict[int, bytes]:      # noqa: N802 - Qt 命名约定
        return {
            self.IdRole: b"recordId",
            self.KindRole: b"kind",
            self.StatusRole: b"status",
            self.PromptRole: b"prompt",
            self.TimeRole: b"timeText",
            self.ThumbRole: b"thumb",
            self.ResultUrlRole: b"resultUrl",
            self.MediaPathRole: b"mediaPath",
            self.BadgeRole: b"badge",
            self.DurationRole: b"durationText",
            self.ErrorRole: b"errorText",
            self.ParamsRole: b"paramsSummary",
            self.FavoriteRole: b"favorite",
            self.TagsRole: b"tags",
        }

    def rowCount(self, parent=QModelIndex()) -> int:      # noqa: N802
        return 0 if parent.isValid() else len(self._records)

    def data(self, index, role=Qt.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._records)):
            return None
        record = self._records[index.row()]
        if role == self.IdRole:
            return record.id
        if role == self.KindRole:
            return record.kind
        if role == self.StatusRole:
            return record.status
        if role == self.PromptRole:
            return record.prompt or "(无提示词)"
        if role == self.TimeRole:
            return _short_time(record.created_at)
        if role == self.ThumbRole:
            return self._thumbs.get(record.id, "")
        if role == self.ResultUrlRole:
            return record.result_url or ""
        if role == self.MediaPathRole:
            return record.media_path or ""
        if role == self.BadgeRole:
            return "视频" if record.kind == "video" else "图片"
        if role == self.DurationRole:
            return f"{record.duration:.1f}s" if record.duration else ""
        if role == self.ErrorRole:
            return record.error or ""
        if role == self.ParamsRole:
            return _params_summary(record.params)
        if role == self.FavoriteRole:
            return record.favorite
        if role == self.TagsRole:
            return list(record.tags)
        return None

    # ---------------------------------------------------------------- 刷新

    @Slot(str, str, int)
    def reload(self, filter_type: str = "all", keyword: str = "", limit: int = 500) -> None:
        """重新加载（筛选 / 搜索变化时调用）。旧的一批结果会被丢弃。"""
        self._filter = filter_type or "all"
        self._keyword = keyword or ""
        self._generation += 1
        generation = self._generation
        if not self._loading:
            self._loading = True
            self.loadingChanged.emit()
        self._runner.submit(self._load(generation, self._filter, self._keyword, limit))

    async def _load(self, generation: int, filter_type: str, keyword: str, limit: int) -> None:
        records = await asyncio.to_thread(
            self._ctx.history.list, kind=filter_type, keyword=keyword, limit=limit
        )
        thumbs = {
            record.id: _file_url(record.thumb_path)
            for record in records
            if record.thumb_path and Path(record.thumb_path).is_file()
        }
        if generation != self._generation:
            return
        self._loaded.emit(generation, records, thumbs)      # 排队回界面线程

        pending = [
            record
            for record in records
            if record.kind == "image"
            and record.media_path
            and Path(record.media_path).is_file()
            and record.id not in thumbs
        ][: self._prefetch_limit]
        batch: list[tuple[str, str]] = []
        last_flush = time.monotonic()
        for record in pending:
            if generation != self._generation:
                return
            path = await asyncio.to_thread(
                self._ctx.media.thumbnail_for, record.id, record.media_path
            )
            if not path:
                continue
            await asyncio.to_thread(self._ctx.history.update, record.id, thumb_path=str(path))
            if generation != self._generation:
                return
            batch.append((record.id, _file_url(str(path))))
            now = time.monotonic()
            if now - last_flush >= THUMB_FLUSH_INTERVAL:
                self._rowsUpdated.emit(generation, list(batch))
                batch.clear()
                last_flush = now
        if batch:
            self._rowsUpdated.emit(generation, list(batch))

    # ---------------------------------------------------------------- 界面线程槽

    def _apply_loaded(self, generation: int, records: object, thumbs: object) -> None:
        if generation != self._generation:
            return
        self.beginResetModel()
        self._records = list(records)          # type: ignore[arg-type]
        self._thumbs = dict(thumbs)            # type: ignore[arg-type]
        self.endResetModel()
        self._loading = False
        self.loadingChanged.emit()
        self.countChanged.emit()

    def _apply_rows_update(self, generation: int, items: object) -> None:
        if generation != self._generation:
            return
        rows: list[int] = []
        for record_id, thumb_url in items:      # type: ignore[misc]
            if not thumb_url:
                continue
            self._thumbs[record_id] = thumb_url
            for row, record in enumerate(self._records):
                if record.id == record_id:
                    rows.append(row)
                    break
        if rows:
            self.dataChanged.emit(
                self.index(min(rows), 0), self.index(max(rows), 0), [self.ThumbRole]
            )

    # ---------------------------------------------------------------- 操作

    @Slot(str, bool, result=bool)
    def setFavorite(self, record_id: str, favorite: bool) -> bool:      # noqa: N802 - QML 调用
        """收藏 / 取消收藏。收藏即视为「认可」，所以顺手记一次 accept。

        这两个信号是后面「找参考图」和「提示词自更新」的输入，所以走专门的
        `set_feedback()`，并且把动作以事件广播出去（将来可落库、可回放）。
        """
        record = self._ctx.history.set_feedback(
            record_id, favorite=favorite, action="accept" if favorite else None
        )
        if record is None:
            return False
        self._publish_feedback(record, "favorite")
        self._refresh_row(record)
        return True

    @Slot(str, str, result=bool)
    def addTag(self, record_id: str, tag: str) -> bool:                 # noqa: N802
        """给记录加一个标签（保留原有标签，重复的会被去掉）。"""
        record = self._ctx.history.get(record_id)
        if record is None or not str(tag).strip():
            return False
        updated = self._ctx.history.set_feedback(record_id, tags=[*record.tags, tag])
        if updated is None:
            return False
        self._publish_feedback(updated, "tag")
        self._refresh_row(updated)
        return True

    @Slot(str, str, result=bool)
    def removeTag(self, record_id: str, tag: str) -> bool:              # noqa: N802
        record = self._ctx.history.get(record_id)
        if record is None:
            return False
        updated = self._ctx.history.set_feedback(
            record_id, tags=[item for item in record.tags if item != tag]
        )
        if updated is None:
            return False
        self._refresh_row(updated)
        return True

    @Slot(str, "QVariantList", result=bool)
    def setTags(self, record_id: str, tags: list) -> bool:              # noqa: N802
        """整体替换标签（大图查看里删标签后回写用）。空值会被 set_feedback 过滤掉。"""
        updated = self._ctx.history.set_feedback(
            record_id, tags=[str(item) for item in (tags or [])]
        )
        if updated is None:
            return False
        self._publish_feedback(updated, "tags")
        self._refresh_row(updated)
        return True

    @Slot(str, str, result=bool)
    def markAction(self, record_id: str, action: str) -> bool:          # noqa: N802
        """记录用户对结果的处理：accept（留下）/ retry（重做）/ discard（丢弃）。"""
        record = self._ctx.history.set_feedback(record_id, action=action)
        if record is None:
            return False
        self._publish_feedback(record, action)
        self._refresh_row(record)
        return True

    def _publish_feedback(self, record: Record, reason: str) -> None:
        from app.state.events import Event

        self._ctx.bus.emit(
            Event(
                type="feedback.recorded",
                job_id=record.job_id or "",
                payload={
                    "record_id": record.id,
                    "reason": reason,
                    "favorite": record.favorite,
                    "tags": list(record.tags),
                    "last_action": record.last_action,
                },
            )
        )

    def _refresh_row(self, record: Record) -> None:
        """把更新后的记录写回内存并只刷新那一行（列表与画廊共用同一份数据）。"""
        for row, existing in enumerate(self._records):
            if existing.id == record.id:
                self._records[row] = record
                index = self.index(row, 0)
                self.dataChanged.emit(index, index)
                break

    @Slot(str, result="QVariantMap")
    def recordAt(self, record_id: str) -> dict:
        """取一条记录的完整信息（大图查看与「载入参数」用）。"""
        record = self._ctx.history.get(record_id)
        return record.to_dict() if record else {}

    @Slot(int, result=str)
    def recordIdAt(self, row: int) -> str:
        """第 row 行的记录 id（大图查看的前后翻页用）。"""
        if 0 <= row < len(self._records):
            return self._records[row].id
        return ""

    @Slot(str, result=bool)
    def remove(self, record_id: str) -> bool:
        """删除一条记录（连同它的本地缓存文件）。"""
        record = self._ctx.history.delete(record_id)
        if record is None:
            return False
        for path in (record.media_path, record.thumb_path):
            self._ctx.media.remove(path)
        self.reload(self._filter, self._keyword)
        return True

    @Slot(result=int)
    def clearAll(self) -> int:      # noqa: N802 - 供 QML 调用
        """清空历史（含缓存文件）。"""
        records = self._ctx.history.list(limit=10**6)
        for record in records:
            for path in (record.media_path, record.thumb_path):
                self._ctx.media.remove(path)
        self._ctx.history.clear()
        self.reload(self._filter, self._keyword)
        return len(records)


def _file_url(path: str | None) -> str:
    """本地文件 → QML 可用的 file:// URL（带 mtime 版本号，避免换了图还显示旧缓存）。"""
    if not path:
        return ""
    target = Path(path)
    if not target.is_file():
        return ""
    url = QUrl.fromLocalFile(str(target)).toString()
    try:
        return f"{url}?v={int(target.stat().st_mtime)}"
    except OSError:
        return url


def _short_time(timestamp: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(timestamp))


def _params_summary(params: dict) -> str:
    """把参数压成一行摘要（卡片副标题用）。"""
    if not params:
        return ""
    parts: list[str] = []
    if params.get("model"):
        parts.append(str(params["model"]))
    if params.get("size"):
        parts.append(str(params["size"]))
    if params.get("seconds"):
        parts.append(f"{params['seconds']}s")
    if params.get("aspect_ratio"):
        parts.append(str(params["aspect_ratio"]))
    refs = params.get("images") or []
    if refs:
        parts.append(f"参考图 {len(refs)} 张")
    return " · ".join(parts)
