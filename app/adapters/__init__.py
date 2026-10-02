"""UI 与协议层之间的接触面。

界面层要用的协议层数据，一律从这里的方法拿；`app/ui/**` 不许直接 import
`runtime/` `validator/` `schemas/` `contracts/`，也不许在界面组件里现造假数据。
"""

from app.adapters.protocol_client import (
    STATUS_ABORTED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    TERMINAL_STATUSES,
    ProtocolClient,
    ProtocolError,
    Reply,
    mock_reply_text,
)

__all__ = [
    "ProtocolClient",
    "ProtocolError",
    "Reply",
    "mock_reply_text",
    "STATUS_COMPLETED",
    "STATUS_ABORTED",
    "STATUS_FAILED",
    "STATUS_TIMEOUT",
    "STATUS_BUDGET_EXCEEDED",
    "TERMINAL_STATUSES",
]