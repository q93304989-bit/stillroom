"""界面层：QML 视图 + 一薄层 Python 桥。

规则（硬规则 1）：**界面不发网络请求、不写文件、不读 .env**，只通过 `UiBridge`
调服务层；服务层与状态层的所有变化都经信号回来，界面只订阅。

Qt 与 asyncio 的分工：asyncio 事件循环跑在专用线程里（见 `async_runner`），
Qt 事件循环只做渲染与信号槽——两者不互相阻塞，也不需要额外依赖。
"""

from app.ui.async_runner import AsyncRunner
from app.ui.bridge import UiBridge
from app.ui.theme import Theme

__all__ = ["AsyncRunner", "UiBridge", "Theme"]
