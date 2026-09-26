"""图像客户端：请求形态、参考图编码、响应解析、参数校验。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.clients.image_client import (
    IMAGE_SIZES,
    ImageClient,
    ImageRequest,
    encode_reference,
    extract_image_url,
    guess_mime,
)
from app.config.credentials import AgnesCredentials
from app.net.errors import AppError, ResponseFormatError, ValidationError
from app.net.http import HttpClient

CREDS = AgnesCredentials(
    api_key="sk-test", base_url="https://api.agnes-ai.cn/v1", image_model="agnes-image-2.5-flash"
)


def make_client(handler) -> tuple[ImageClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = HttpClient(transport=httpx.MockTransport(wrapped), backoff=0)
    return ImageClient(http, CREDS), seen


async def test_text_to_image_payload_and_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://cdn/img.png"}]})

    client, seen = make_client(handler)
    url = await client.generate(ImageRequest(prompt="一只猫", size="1024x1024"))

    assert url == "https://cdn/img.png"
    request = seen[0]
    assert str(request.url) == "https://api.agnes-ai.cn/v1/images/generations"
    # 图像接口用裸 key，不是 Bearer
    assert request.headers["Authorization"] == "sk-test"
    body = json.loads(request.content)
    assert body["model"] == "agnes-image-2.5-flash"
    assert body["prompt"] == "一只猫"
    assert body["size"] == "1024x1024"
    assert body["extra_body"] == {"response_format": "url"}
    assert "image" not in body["extra_body"]


async def test_local_reference_becomes_data_uri(tmp_path):
    image_file = tmp_path / "ref.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://cdn/out.png"}]})

    client, seen = make_client(handler)
    await client.generate(ImageRequest(prompt="改风格", images=(str(image_file),)))

    body = json.loads(seen[0].content)
    ref = body["extra_body"]["image"][0]
    assert ref.startswith("data:image/png;base64,")
    assert base64.b64decode(ref.split(",", 1)[1]) == image_file.read_bytes()


async def test_public_url_reference_passed_through():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://cdn/out.png"}]})

    client, seen = make_client(handler)
    await client.generate(
        ImageRequest(prompt="x", images=("https://example.com/a.jpg", "  "))
    )
    body = json.loads(seen[0].content)
    assert body["extra_body"]["image"] == ["https://example.com/a.jpg"]


async def test_invalid_size_rejected_before_request():
    client, seen = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValidationError) as excinfo:
        await client.generate(ImageRequest(prompt="x", size="999x999"))
    assert "尺寸" in str(excinfo.value)
    assert not seen  # 校验失败就不该发请求


async def test_empty_prompt_rejected():
    client, _ = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValidationError):
        await client.generate(ImageRequest(prompt="   "))


async def test_too_many_references_rejected(tmp_path):
    files = []
    for i in range(6):
        path = tmp_path / f"r{i}.png"
        path.write_bytes(b"x")
        files.append(str(path))
    client, _ = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValidationError) as excinfo:
        await client.generate(ImageRequest(prompt="x", images=tuple(files)))
    assert "最多 5 张" in str(excinfo.value)


async def test_missing_key_raises_config_error():
    client = ImageClient(
        HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
        AgnesCredentials(api_key="", base_url="https://api.agnes-ai.cn/v1"),
    )
    from app.net.errors import ConfigError

    with pytest.raises(ConfigError):
        await client.generate(ImageRequest(prompt="x"))


def test_extract_image_url_variants():
    assert extract_image_url({"data": [{"url": "u"}]}) == "u"
    assert extract_image_url({"data": [{"image_url": "u2"}]}) == "u2"
    with pytest.raises(ResponseFormatError):
        extract_image_url({"data": []})
    with pytest.raises(AppError):
        extract_image_url({"error": "模型不可用"})


def test_guess_mime_defaults_to_png():
    assert guess_mime("a.JPG") == "image/jpeg"
    assert guess_mime("noext") == "image/png"


def test_all_seven_sizes_present():
    assert IMAGE_SIZES == [
        "1024x768",
        "512x512",
        "768x1024",
        "1024x1024",
        "1280x720",
        "720x1280",
        "1920x1080",
    ]


def test_encode_reference_rejects_missing_file():
    with pytest.raises(ValidationError):
        encode_reference(r"Z:\definitely\missing.png")
