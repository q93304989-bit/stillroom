"""服务层：把「一次生成」从提交到落盘编排起来。

- `GenerationService`：任务编排（限流 → 调用 → 重试 → 进度 → 取消 → 落历史）
- `RateLimiter`：按能力的平台限额排队
- `HistoryStore`：SQLite 历史（记录 + 资产）
- `MediaStore`：下载与缓存产物

服务层不依赖任何 GUI，也不直接读 `.env`（配置从上面注入），因此可以整体无界面测试。
"""

from app.services.clock import Clock, SystemClock, VirtualClock
from app.services.generation import GenerationService
from app.services.history import HistoryStore, Record
from app.services.media import MediaStore
from app.services.rate_limit import RateLimiter

__all__ = [
    "Clock",
    "SystemClock",
    "VirtualClock",
    "GenerationService",
    "HistoryStore",
    "Record",
    "MediaStore",
    "RateLimiter",
]
