"""出网层：统一 HTTP 客户端 + 统一错误分类。

两条规则：

- 所有网络请求都经 `HttpClient`，模块内不再直接使用 `requests` / `httpx`；
- 所有失败都转成 `app.net.errors` 里的类型化异常，界面只做文案映射。
"""

from app.net import errors
from app.net.http import HttpClient, NetworkMode

__all__ = ["errors", "HttpClient", "NetworkMode"]
