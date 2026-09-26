"""HttpClient 行为：错误分类、直连→代理换道、网络模式、JSON 解析。"""

from __future__ import annotations

import httpx
import pytest

from app.net.errors import AuthError, NetworkError, QueueFullError, ResponseFormatError
from app.net.http import HttpClient, NetworkMode, attempts_for


def make_client(handler, **kwargs) -> HttpClient:
    kwargs.setdefault("backoff", 0)
    return HttpClient(transport=httpx.MockTransport(handler), **kwargs)


def test_attempt_orders():
    assert attempts_for(NetworkMode.DIRECT) == (False,)
    assert attempts_for(NetworkMode.PROXY) == (True,)
    assert attempts_for(NetworkMode.AUTO) == (False, False, True)


async def test_success_path_returns_json():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://img/1.png"}]})

    client = make_client(handler)
    data = await client.post_json("https://api.test/images/generations", json={"prompt": "x"})

    assert data["data"][0]["url"] == "https://img/1.png"
    assert len(calls) == 1
    await client.aclose()


async def test_http_error_is_classified_not_retried():
    """401 属于配置类硬故障：一次就抛，绝不重试（旧版在这里傻等 15 分钟）。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"message": "Invalid token"})

    client = make_client(handler)
    with pytest.raises(AuthError):
        await client.get_json("https://api.test/agnesapi")

    assert len(calls) == 1
    await client.aclose()


async def test_queue_full_is_classified():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "video_queue_full"})

    client = make_client(handler)
    with pytest.raises(QueueFullError):
        await client.post_json("https://api.test/videos", json={})
    await client.aclose()


async def test_connection_error_falls_back_to_proxy_in_auto_mode():
    """直连失败 → 换道重试（旧版 adaptive_request 的核心策略）。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(True)
        if len(seen) < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    data = await client.get_json("https://api.test/models")

    assert data == {"ok": True}
    assert len(seen) == 3          # 两次直连 + 一次代理
    await client.aclose()


async def test_direct_mode_does_not_fall_back():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(True)
        raise httpx.ConnectError("nope", request=request)

    client = make_client(handler, network_mode="direct")
    with pytest.raises(NetworkError):
        await client.get_json("https://api.test/models")

    assert len(seen) == 1
    await client.aclose()


async def test_network_mode_switch_changes_attempts():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(True)
        if len(seen) == 1:
            raise httpx.ConnectError("flaky", request=request)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler, network_mode="direct")
    with pytest.raises(NetworkError):
        await client.get_json("https://api.test/models")

    client.set_network_mode("auto")
    assert await client.get_json("https://api.test/models") == {"ok": True}
    await client.aclose()


async def test_non_json_response_raises_format_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = make_client(handler)
    with pytest.raises(ResponseFormatError):
        await client.get_json("https://api.test/models")
    await client.aclose()
