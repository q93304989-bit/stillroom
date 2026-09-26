"""视频客户端：端点、模式切换、平台限制、flash 模型的 model_name 规则。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.clients.video_client import (
    FLASH_VIDEO_MODELS,
    VideoClient,
    VideoRequest,
    extract_video_id,
    validate_reference_images,
)
from app.config.credentials import AgnesCredentials
from app.net.errors import ValidationError
from app.net.http import HttpClient

CREDS = AgnesCredentials(
    api_key="sk-video", base_url="https://api.agnes-ai.cn/v1", video_model="agnes-video-2.5-flash"
)


def make_client(handler) -> tuple[VideoClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = HttpClient(transport=httpx.MockTransport(wrapped), backoff=0)
    return VideoClient(http, CREDS), seen


async def test_submit_text_mode_payload():
    client, seen = make_client(
        lambda r: httpx.Response(200, json={"video_id": "vid-1", "status": "queued"})
    )
    data = await client.submit(VideoRequest(prompt="海面日落", seconds="8", aspect_ratio="9:16"))

    assert extract_video_id(data) == "vid-1"
    request = seen[0]
    assert str(request.url) == "https://api.agnes-ai.cn/v1/videos"
    assert request.headers["Authorization"] == "Bearer sk-video"  # 视频接口用 Bearer
    body = json.loads(request.content)
    assert body["mode"] == "text"
    assert body["size"] == "720P"
    assert body["seconds"] == "8"
    assert body["aspect_ratio"] == "9:16"
    assert "images" not in body


async def test_submit_reference_mode_includes_images():
    client, seen = make_client(lambda r: httpx.Response(200, json={"video_id": "vid-2"}))
    await client.submit(
        VideoRequest(prompt="让它动起来", images=("https://cdn/a.png",), seed=7)
    )
    body = json.loads(seen[0].content)
    assert body["mode"] == "reference"
    assert body["images"] == ["https://cdn/a.png"]
    assert body["seed"] == 7


async def test_local_reference_rejected_with_actionable_message():
    with pytest.raises(ValidationError) as excinfo:
        validate_reference_images([r"C:\pics\a.png"])
    message = excinfo.value.message          # 用户可见文案（str() 会带上类名，供日志用）
    assert "http(s) URL" in message
    assert "图床" in message
    assert "图生图" in message


async def test_query_uses_agnesapi_origin_and_omits_model_name_for_non_flash():
    client, seen = make_client(lambda r: httpx.Response(200, json={"status": "running"}))
    await client.query("vid-3", model_name="agnes-video-2.5")

    request = seen[0]
    assert str(request.url).startswith("https://api.agnes-ai.cn/agnesapi?")
    assert "video_id=vid-3" in str(request.url)
    assert "model_name" not in str(request.url)


async def test_query_adds_model_name_for_flash_models():
    assert "agnes-video-2.5-flash" in FLASH_VIDEO_MODELS
    client, seen = make_client(lambda r: httpx.Response(200, json={"status": "running"}))
    await client.query("vid-4", model_name="agnes-video-2.5-flash")
    assert "model_name=agnes-video-2.5-flash" in str(seen[0].url)


async def test_query_requires_video_id():
    client, seen = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValidationError):
        await client.query("  ")
    assert not seen


@pytest.mark.parametrize(
    "seconds,aspect",
    [("7", "16:9"), ("5", "2:1")],
)
async def test_invalid_options_rejected(seconds, aspect):
    client, seen = make_client(lambda r: httpx.Response(200, json={}))
    request = VideoRequest(prompt="x", seconds=seconds, aspect_ratio=aspect)
    with pytest.raises(ValidationError):
        await client.submit(request)
    assert not seen


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"video_id": "a"}, "a"),
        ({"id": "b"}, "b"),
        ({"task_id": "c"}, "c"),
        ({"data": {"video_id": "d"}}, "d"),
        ({"result": {"task_id": "e"}}, "e"),
        ({"nope": 1}, ""),
    ],
)
def test_extract_video_id_variants(payload, expected):
    assert extract_video_id(payload) == expected
