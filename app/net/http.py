"""统一出网客户端（asyncio + httpx）。

继承旧版 `http_session.adaptive_request` 的核心策略——**直连优先、失败自动改走系统
代理**（本机到境外接口偶发 SSL 被掐断，需要换道自愈），并在其基础上做了三件事：

1. 全异步，避免用线程池做网络 IO；
2. 连接类失败才换道重试，HTTP 状态码一律交给 `classify_http` 分类，不在这里重试
   （重试策略属于编排层，Phase 2 的调度器统一决定）；
3. 请求可注入 `transport`，测试用 `httpx.MockTransport` 就能覆盖全部分支，不发真实请求。
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Mapping

import httpx

from app.net.errors import (
    AppError,
    NetworkError,
    RequestTimeout,
    classify_exception,
    classify_http,
)

DEFAULT_UA = "AgnesStudio/0.1 (+https://github.com/q93304989-bit/AgnesStudio)"


class NetworkMode(str, Enum):
    """网络模式（与旧版设置页的三个选项一一对应）。"""

    AUTO = "auto"      # 直连优先，失败换系统代理
    DIRECT = "direct"  # 仅直连
    PROXY = "proxy"    # 仅系统代理

    @classmethod
    def parse(cls, value: str | "NetworkMode" | None) -> "NetworkMode":
        if isinstance(value, NetworkMode):
            return value
        try:
            return cls(str(value or "auto").lower())
        except ValueError:
            return cls.AUTO


def attempts_for(mode: NetworkMode) -> tuple[bool, ...]:
    """返回尝试顺序；布尔值表示「是否信任环境代理（trust_env）」。"""
    if mode is NetworkMode.DIRECT:
        return (False,)
    if mode is NetworkMode.PROXY:
        return (True,)
    return (False, False, True)  # 直连两次（抗瞬时抖动）→ 系统代理


class HttpClient:
    """带自适应网络模式的异步 HTTP 客户端。

    生命周期：在 `bootstrap` 里创建一个实例，注入给各客户端；退出时 `aclose()`。
    """

    def __init__(
        self,
        *,
        network_mode: str | NetworkMode = NetworkMode.AUTO,
        timeout: float = 30.0,
        backoff: float = 1.0,
        user_agent: str = DEFAULT_UA,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._mode = NetworkMode.parse(network_mode)
        self._timeout = timeout
        self._backoff = backoff
        self._user_agent = user_agent
        self._transport = transport
        self._clients: dict[bool, httpx.AsyncClient] = {}

    # ---------------------------------------------------------------- 生命周期

    @property
    def network_mode(self) -> NetworkMode:
        return self._mode

    def set_network_mode(self, mode: str | NetworkMode) -> NetworkMode:
        """切换网络模式（设置页调用）。不重建连接池，只是一次请求换一次顺序。"""
        self._mode = NetworkMode.parse(mode)
        return self._mode

    def _client(self, trust_env: bool) -> httpx.AsyncClient:
        client = self._clients.get(trust_env)
        if client is None:
            client = httpx.AsyncClient(
                trust_env=trust_env,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            )
            self._clients[trust_env] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- 请求

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        files: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """发一次请求，返回 2xx 响应；否则抛类型化异常。"""
        mode = self._mode
        attempts = attempts_for(mode)
        last_error: AppError | None = None

        for index, trust_env in enumerate(attempts):
            client = self._client(trust_env)
            try:
                response = await client.request(
                    method,
                    url,
                    headers=dict(headers or {}),
                    params=dict(params or {}),
                    json=json,
                    content=content,
                    files=files,
                    timeout=timeout or self._timeout,
                )
            except (httpx.HTTPError, OSError) as exc:
                last_error = classify_exception(exc, endpoint=url)
                if index < len(attempts) - 1:
                    await asyncio.sleep(self._backoff * (index + 1))
                    continue
                raise last_error from exc

            if response.status_code >= 400:
                raise classify_http(
                    response.status_code,
                    body=_safe_body(response),
                    endpoint=url,
                    headers=response.headers,
                )
            return response

        raise last_error or NetworkError("请求失败：没有可用的网络通道", endpoint=url)

    async def get_json(self, url: str, **kwargs) -> Any:
        response = await self.request("GET", url, **kwargs)
        return _parse_json(response, url)

    async def post_json(self, url: str, **kwargs) -> Any:
        response = await self.request("POST", url, **kwargs)
        return _parse_json(response, url)

    async def put_json(self, url: str, **kwargs) -> Any:
        response = await self.request("PUT", url, **kwargs)
        return _parse_json(response, url)

    async def delete_json(self, url: str, **kwargs) -> Any:
        response = await self.request("DELETE", url, **kwargs)
        return _parse_json(response, url)

    async def download(self, url: str, *, timeout: float | None = None) -> bytes:
        """下载二进制内容（图片 / 视频）。"""
        response = await self.request("GET", url, timeout=timeout)
        return response.content


def _safe_body(response: httpx.Response) -> Any:
    """尽量取出可读的错误详情（JSON 优先，其次截断文本）。"""
    try:
        return response.json()
    except Exception:
        try:
            return response.text[:200]
        except Exception:  # pragma: no cover - 极端情况下的兜底
            return None


def _parse_json(response: httpx.Response, url: str) -> Any:
    from app.net.errors import ResponseFormatError

    try:
        return response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"响应不是合法 JSON：{exc}", endpoint=url) from exc
