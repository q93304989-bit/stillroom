"""QML 用的数据模型。

模型归界面层，但它不碰网络也不读 `.env`：数据来自服务层（SQLite 与媒体目录），
重活（查库、生成缩略图）都提交给 asyncio 线程，结果用 Qt 信号排队回界面线程。
"""

from app.ui.models.history_model import HistoryModel

__all__ = ["HistoryModel"]
