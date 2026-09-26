"""联网搜索客户端：provider 请求形状、响应解析、以及「搜不了」的各种说清。

这一层的验收标准不是「能搜到东西」（那取决于外部服务），而是：
**每一种失败都能被用户看懂**，且关掉 / 没 key 时一次网络请求都不发。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.clients import search_client as search_module
from app.clients.search_client import SearchClient
from app.config.credentials import SearchCredentials
from app.net.errors import ResponseFormatError, ValidationError
from app.net.http import HttpClient


def creds(provider: str = "tavily", key: str = "sk-search", base_url: str = "") -> SearchCredentials:
    return SearchCredentials(provider=provider, api_key=key, base_url=base_url)


def make_client(handler, tmp_path=None, **kwargs) -> tuple[SearchClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = HttpClient(transport=httpx.MockTransport(wrapped), backoff=0)
    client = SearchClient(
        http,
        kwargs.pop("credentials", creds()),
        image_dir=tmp_path / "web_refs" if tmp_path else None,
        **kwargs,
    )
    return client, seen


def test_provider_endpoints():
    assert creds("tavily").endpoint == "https://api.tavily.com/search"
    assert creds("bocha").endpoint == "https://api.bochaai.com/v1/web-search"
    assert creds("serper").endpoint == "https://google.serper.dev/search"
    assert creds("custom", base_url="https://my-search.example/v1").endpoint == "https://my-search.example/v1/search"


def test_unavailable_reasons_are_specific():
    off = SearchCredentials(provider="off", api_key="sk")
    assert not off.enabled and not off.configured
    assert "off" in off.unavailable_reason()

    missing_key = SearchCredentials(provider="tavily", api_key="")
    assert "SEARCH_API_KEY" in missing_key.unavailable_reason()

    custom = SearchCredentials(provider="custom", api_key="sk", base_url="")
    assert "SEARCH_BASE_URL" in custom.unavailable_reason()

    assert SearchCredentials(provider="tavily", api_key="sk").unavailable_reason() == ""


async def test_missing_key_sends_no_request():
    client, seen = make_client(
        lambda r: httpx.Response(500), credentials=SearchCredentials(provider="tavily", api_key="")
    )
    result = await client.search("中秋国潮海报")

    assert result.ok is False
    assert result.count == 0
    assert "SEARCH_API_KEY" in result.reason
    assert seen == []                     # 没配 key 就绝不发请求


async def test_provider_off_sends_no_request():
    client, seen = make_client(
        lambda r: httpx.Response(500), credentials=SearchCredentials(provider="off", api_key="sk")
    )
    result = await client.search("中秋国潮海报")

    assert result.ok is False and seen == []


async def test_tavily_request_and_parse():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["query"] == "国潮海报"
        assert body["api_key"] == "sk-search"
        assert body["include_images"] is True
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "国潮配色指南", "url": "https://a.example/1", "content": "红金为主，压饱和", "score": 0.91},
                    {"title": "无链接结果", "content": "应当被丢掉"},
                ],
                "images": ["https://img.example/a.jpg", {"url": "https://img.example/b.png"}],
            },
        )

    client, seen = make_client(handler)
    result = await client.search("国潮海报", want_images=True)

    assert str(seen[0].url) == "https://api.tavily.com/search"
    assert seen[0].headers["Authorization"] == "Bearer sk-search"
    assert result.ok is True
    assert result.count == 1                       # 没有 url 的那条被丢掉
    assert result.items[0].snippet == "红金为主，压饱和"
    assert result.images == ["https://img.example/a.jpg", "https://img.example/b.png"]
    assert "2 条" not in result.reason and "1 条" in result.reason


async def test_tavily_skips_images_when_not_wanted():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "include_images" not in body       # 开关关着，连候选图都不请求
        return httpx.Response(
            200,
            json={
                "results": [{"title": "t", "url": "https://a.example/1", "content": "c"}],
                "images": ["https://img.example/a.jpg"],
            },
        )

    client, _ = make_client(handler)
    result = await client.search("国潮海报")
    assert result.images == []


async def test_bocha_parse():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.bochaai.com/v1/web-search"
        return httpx.Response(
            200,
            json={
                "data": {
                    "webPages": {
                        "value": [
                            {"name": "国潮设计", "url": "https://b.example/1", "summary": "红金留白", "siteName": "站酷"}
                        ]
                    }
                }
            },
        )

    client, _ = make_client(handler, credentials=creds("bocha"))
    result = await client.search("国潮")

    assert result.items[0].title == "国潮设计"
    assert result.items[0].source == "站酷"


async def test_serper_uses_api_key_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == "sk-search"
        return httpx.Response(
            200, json={"organic": [{"title": "Serper 结果", "link": "https://c.example/1", "snippet": "摘要"}]}
        )

    client, _ = make_client(handler, credentials=creds("serper"))
    result = await client.search("国潮")
    assert result.items[0].url == "https://c.example/1"


async def test_custom_provider_parse():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://my.example/v1/search"
        return httpx.Response(
            200, json={"results": [{"name": "自建结果", "link": "https://d.example/1", "text": "内容"}]}
        )

    client, _ = make_client(handler, credentials=creds("custom", base_url="https://my.example/v1"))
    result = await client.search("国潮")
    assert result.items[0].title == "自建结果"


async def test_http_error_becomes_reason():
    client, _ = make_client(lambda r: httpx.Response(401, json={"message": "invalid api key"}))
    result = await client.search("国潮")

    assert result.ok is False
    assert "联网搜索失败" in result.reason and "invalid api key" in result.reason


async def test_empty_result_reason():
    client, _ = make_client(lambda r: httpx.Response(200, json={"results": []}))
    result = await client.search("国潮")

    assert result.ok is True                # 真搜了，只是没结果
    assert result.count == 0
    assert "没有能用的结果" in result.reason


async def test_blank_query_rejected_without_request():
    client, seen = make_client(lambda r: httpx.Response(200, json={"results": []}))
    result = await client.search("   ")

    assert result.ok is False and seen == []


async def test_fetch_image_saves_to_web_refs(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    client, seen = make_client(lambda r: httpx.Response(200, content=png), tmp_path)
    path = await client.fetch_image("https://img.example/a.png")

    assert path.parent == tmp_path / "web_refs"
    assert path.suffix == ".png"
    assert path.read_bytes() == png
    assert len(seen) == 1


async def test_fetch_image_same_url_same_file(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    client, _ = make_client(lambda r: httpx.Response(200, content=png), tmp_path)
    first = await client.fetch_image("https://img.example/a.png")
    second = await client.fetch_image("https://img.example/a.png")
    assert first == second                 # 同一个 URL 不重复占空间


async def test_fetch_image_rejects_non_image(tmp_path):
    client, _ = make_client(lambda r: httpx.Response(200, content=b"<html>not an image</html>"), tmp_path)
    with pytest.raises(ResponseFormatError):
        await client.fetch_image("https://img.example/page")


async def test_fetch_image_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(search_module, "MAX_IMAGE_BYTES", 100)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
    client, _ = make_client(lambda r: httpx.Response(200, content=png), tmp_path)
    with pytest.raises(ValidationError):
        await client.fetch_image("https://img.example/big.png")


async def test_fetch_image_needs_image_dir():
    client = SearchClient(HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))), creds())
    with pytest.raises(ValidationError):
        await client.fetch_image("https://img.example/a.png")


def test_images_deduped_and_limited():
    rows = [{"url": f"https://img.example/{i}.png"} for i in range(10)]
    rows.append({"url": "https://img.example/0.png"})
    assert search_module._images_of(rows) == [f"https://img.example/{i}.png" for i in range(4)]
