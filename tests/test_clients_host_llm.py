"""图床与 LLM 客户端：路由、默认分支探测、错误处理、响应解析。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.clients.image_host import (
    GH_API,
    SEE_UPLOAD_URL,
    extract_see_url,
    upload_to_github,
    upload_to_image_host,
    upload_to_see,
)
from app.clients.llm_client import LlmClient, extract_chat_text
from app.config.credentials import GitHubCredentials, LlmCredentials, SeeCredentials
from app.net.errors import AuthError, ConfigError, ResponseFormatError, ValidationError
from app.net.http import HttpClient

GH = GitHubCredentials(token="ghp_x", repo="me/images")
SEE = SeeCredentials(token="see-key")


def make_http(handler) -> tuple[HttpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return HttpClient(transport=httpx.MockTransport(wrapped), backoff=0), seen


async def test_github_upload_looks_up_default_branch_then_puts(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"bytes")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(200, json={"default_branch": "trunk"})
        return httpx.Response(201, json={"content": {"download_url": "https://raw/x/a.png"}})

    http, _ = make_http(handler)
    url = await upload_to_github(http, GH, image, timestamp=1700000000)

    assert url == "https://raw/x/a.png"
    assert calls[0] == "GET /repos/me/images"
    assert calls[1].startswith("PUT /repos/me/images/contents/agnes-refs/1700000000000_a.png")


async def test_github_upload_falls_back_to_raw_url(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(201, json={})

    http, _ = make_http(handler)
    url = await upload_to_github(http, GH, image, timestamp=1700000000)
    assert url == "https://raw.githubusercontent.com/me/images/main/agnes-refs/1700000000000_a.png"


async def test_github_upload_requires_config(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    http, seen = make_http(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ConfigError) as excinfo:
        await upload_to_github(http, GitHubCredentials(), image)
    assert "GITHUB_TOKEN" in str(excinfo.value)
    assert not seen


async def test_github_upload_rejects_large_file(tmp_path):
    image = tmp_path / "big.png"
    image.write_bytes(b"0" * (6 * 1024 * 1024))
    http, _ = make_http(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValidationError) as excinfo:
        await upload_to_github(http, GH, image)
    assert "5MB" in str(excinfo.value)


async def test_github_invalid_token_becomes_auth_error_with_guide(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    http, _ = make_http(lambda r: httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(AuthError) as excinfo:
        await upload_to_github(http, GH, image)
    assert "重新生成" in str(excinfo.value)


async def test_see_upload_posts_multipart_and_reads_url(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"bytes")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"url": "https://s.ee/a.png"}})

    http, _ = make_http(handler)
    url = await upload_to_see(http, SEE, image)

    assert url == "https://s.ee/a.png"
    assert str(seen[0].url) == SEE_UPLOAD_URL
    assert seen[0].headers["Authorization"] == "see-key"
    assert b'name="file"' in seen[0].content


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"data": {"url": "u1"}}, "u1"),
        ({"images": ["u2"]}, "u2"),
        ({"images": "u3"}, "u3"),
    ],
)
def test_extract_see_url_variants(payload, expected):
    assert extract_see_url(payload) == expected


def test_extract_see_url_error_message():
    with pytest.raises(ValidationError):
        extract_see_url({"message": "quota exceeded"})


async def test_auto_router_prefers_github_when_both_configured(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(201, json={"content": {"download_url": "https://raw/gh"}})

    http, seen = make_http(handler)
    url = await upload_to_image_host(http, GH, SEE, image)
    assert url == "https://raw/gh"
    assert GH_API in str(seen[0].url)


async def test_auto_router_uses_see_when_github_missing(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    http, _ = make_http(lambda r: httpx.Response(200, json={"data": {"url": "https://s.ee/u"}}))
    url = await upload_to_image_host(http, GitHubCredentials(), SEE, image)
    assert url == "https://s.ee/u"


async def test_auto_router_without_config_gives_both_guides(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    http, _ = make_http(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ConfigError) as excinfo:
        await upload_to_image_host(http, GitHubCredentials(), SeeCredentials(), image)
    message = str(excinfo.value)
    assert "GITHUB_TOKEN" in message and "SEE_API_TOKEN" in message


# --------------------------------------------------------------------------- LLM

LLM_CREDS = LlmCredentials(api_key="sk-llm", base_url="https://llm.example/v1", model="m1")


async def test_llm_chat_payload_and_extraction():
    http, seen = make_http(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]})
    )
    client = LlmClient(http, LLM_CREDS)
    text = await client.chat([{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=32)

    assert text == "你好"
    body = json.loads(seen[0].content)
    assert body["model"] == "m1"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 32
    assert str(seen[0].url) == "https://llm.example/v1/chat/completions"


async def test_llm_requires_config():
    http, seen = make_http(lambda r: httpx.Response(200, json={}))
    client = LlmClient(http, LlmCredentials())
    with pytest.raises(ConfigError):
        await client.chat([{"role": "user", "content": "hi"}])
    assert not seen


async def test_llm_models_list():
    http, _ = make_http(
        lambda r: httpx.Response(200, json={"data": [{"id": "a"}, {"name": "b"}, {"x": 1}]})
    )
    client = LlmClient(http, LLM_CREDS)
    assert await client.models() == ["a", "b"]


def test_extract_chat_text_fallbacks():
    assert extract_chat_text({"choices": [{"text": "t"}]}) == "t"
    with pytest.raises(ResponseFormatError):
        extract_chat_text({"choices": []})
