"""视觉描述客户端：图片归一化、请求形状、JSON 解析与失败降级信号。"""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from app.clients.vision_client import (
    DEFAULT_MAX_TOKENS,
    VisionClient,
    extract_json_object,
)
from app.config.credentials import VisionCredentials
from app.net.errors import ConfigError, ResponseFormatError, ValidationError
from app.net.http import HttpClient

CREDS = VisionCredentials(
    provider="deepseek",
    api_key="sk-vision",
    base_url="https://api.deepseek.com",
    model="deepseek-v4-flash-vision-exp",
)

GOOD_JSON = json.dumps(
    {
        "subject": "未来城市夜景",
        "style": "赛博朋克",
        "composition": "中心对称",
        "lighting": "霓虹冷调",
        "flaws": ["文字不可读", "人物模糊"],
    },
    ensure_ascii=False,
)


def make_image(tmp_path, size=(900, 600)) -> str:
    path = tmp_path / "big.png"
    Image.new("RGB", size, (30, 90, 200)).save(path)
    return str(path)


def make_client(handler) -> tuple[VisionClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = HttpClient(transport=httpx.MockTransport(wrapped), backoff=0)
    return VisionClient(http, CREDS), seen


def ok_response(content: str = GOOD_JSON) -> httpx.Response:
    return httpx.Response(
        200, json={"model": CREDS.model, "choices": [{"message": {"content": content}}], "usage": {"total_tokens": 623}}
    )


def test_local_image_is_downscaled_and_inlined(tmp_path):
    client, _ = make_client(lambda r: ok_response())
    url = client.prepare_image(make_image(tmp_path))

    assert url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert max(image.size) <= 320          # 实测：320px 比原图快 14.5 倍


def test_public_url_is_passed_through():
    client, _ = make_client(lambda r: ok_response())
    image = "https://cdn.example/a.png"
    assert client.prepare_image(image) == image


def test_payload_shape_includes_max_tokens_and_text_block(tmp_path):
    client, _ = make_client(lambda r: ok_response())
    payload = client.build_payload(make_image(tmp_path), requirement="中秋海报")

    assert payload["model"] == CREDS.model
    assert payload["max_tokens"] == DEFAULT_MAX_TOKENS >= 3000      # 给少了正文会空
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "中秋海报" in content[0]["text"]
    assert content[1]["type"] == "image_url"


async def test_describe_parses_json(tmp_path):
    client, seen = make_client(lambda r: ok_response())
    description = await client.describe(make_image(tmp_path))

    assert description.subject == "未来城市夜景"
    assert description.flaws == ("文字不可读", "人物模糊")
    assert description.model == CREDS.model
    assert description.has_content
    assert description.to_state()["subject"] == "未来城市夜景"
    assert str(seen[0].url) == "https://api.deepseek.com/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer sk-vision"


async def test_describe_tolerates_code_fence(tmp_path):
    client, _ = make_client(lambda r: ok_response(f"```json\n{GOOD_JSON}\n```"))
    assert (await client.describe(make_image(tmp_path))).style == "赛博朋克"


async def test_empty_content_raises_format_error(tmp_path):
    """max_tokens 给小了会被 reasoning 吃光、正文为空——必须能被上层识别。"""
    client, _ = make_client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
    )
    with pytest.raises(ResponseFormatError) as excinfo:
        await client.describe(make_image(tmp_path))
    assert "max_tokens" in str(excinfo.value)


async def test_non_json_content_raises(tmp_path):
    client, _ = make_client(lambda r: ok_response("这张图很好看，我很喜欢。"))
    with pytest.raises(ResponseFormatError):
        await client.describe(make_image(tmp_path))


async def test_missing_key_gives_config_error(tmp_path):
    client = VisionClient(
        HttpClient(transport=httpx.MockTransport(lambda r: ok_response())),
        VisionCredentials(provider="deepseek", api_key="", base_url="https://api.deepseek.com", model="m"),
    )
    with pytest.raises(ConfigError):
        await client.describe(make_image(tmp_path))


def test_missing_file_rejected(tmp_path):
    client, _ = make_client(lambda r: ok_response())
    with pytest.raises(ValidationError):
        client.prepare_image(str(tmp_path / "不存在.png"))


def test_flaws_as_string_becomes_tuple(tmp_path):
    client, _ = make_client(
        lambda r: ok_response(json.dumps({"subject": "x", "flaws": "只有一条"}, ensure_ascii=False))
    )
    payload = client.build_payload(make_image(tmp_path))
    description = client._parse(
        {"choices": [{"message": {"content": json.dumps({"subject": "x", "flaws": "只有一条"}, ensure_ascii=False)}}]},
        0.1,
    )
    assert description.flaws == ("只有一条",)
    assert payload  # 保持接口被调用过


def test_extract_json_object_variants():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('前言 {"a": 2} 后语') == {"a": 2}
    assert extract_json_object("没有 JSON") == {}
