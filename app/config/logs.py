"""意外异常的落盘记录。

界面打包成窗口程序后**没有控制台**：一旦运行里冒出没人预料到的异常，用户看到的只有
「点了没反应」，我们这边则什么都没有。所以凡是「按理不该发生」的异常，除了在界面上
说清楚，也往数据目录里的 `logs/error.log` 追一段（时间 / 位置 / 类型 / 消息 / 调用栈）。

它不是日志系统，只是**出事以后的现场**：正经的用户可见信息仍然走界面。
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path

from app.config import paths, settings


def error_log_path() -> Path:
    """`<数据目录>/logs/error.log`（设置页的「打开目录」能直接翻到它）。

    数据目录按应用一致的顺序解析（环境变量 > 设置 > 默认），用户改过目录也能找到日志。
    """
    return settings.data_dir() / "logs" / "error.log"


def log_error(where: str, exc: BaseException) -> None:
    """把一次意外异常追加进日志；写不进去也绝不连累主流程。"""
    try:
        path = error_log_path()
        paths.ensure_dir(path.parent)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {stamp} · {where} ===\n{detail}")
    except Exception:                      # pragma: no cover - 日志写失败不该再炸一次
        pass
