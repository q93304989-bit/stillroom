"""能力注册表：声明内容、平台限制进元数据、MCP 形态、调用通路。"""

from __future__ import annotations

import httpx
import pytest

from app.capabilities.registry import (
    SIDE_DISK_READ,
    SIDE_DISK_WRITE,
    SIDE_NETWORK,
    SIDE_PAID,
    SIDE_UPLOAD,
    ToolRegistry,
    ToolSpec,
    build_registry,
)
from app.clients.image_client import IMAGE_MODELS, IMAGE_SIZES
from app.clients.video_client import ASPECT_RATIOS, SECONDS_OPTIONS, VIDEO_MODELS
from app.config.credentials import (
    AgnesCredentials,
    Credentials,
    GitHubCredentials,
    LlmCredentials,
    SeeCredentials,
    TypeSafeCredentials,
    VisionCredentials,
)
from app.net.errors import AppError
from app.net.http import HttpClient


def make_credentials() -> Credentials:
    return Credentials(
        agnes=AgnesCredentials(api_key="sk-a", base_url="https://api.agnes-ai.cn/v1"),
        github=GitHubCredentials(),
        see=SeeCredentials(),
        llm=LlmCredentials(),
        typesafe=TypeSafeCredentials(api_key="apikey-test", base_url="https://api.typesafe.ai"),
        vision=VisionCredentials(
            provider="deepseek",
            api_key="sk-vision",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash-vision-exp",
        ),
    )


def make_registry(handler=None) -> ToolRegistry:
    http = HttpClient(
        transport=httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={}))),
        backoff=0,
    )
    credentials = make_credentials()
    # 传一个临时历史库：这样 rag.search 也会被注册（bootstrap 里也是这个顺序）
    import tempfile
    from pathlib import Path

    from app.services.history import HistoryStore

    history = HistoryStore(Path(tempfile.mkdtemp(prefix="regtest_")) / "h.db")
    registry = build_registry(http=http, credentials=credentials, history=history)
    registry._test_history = history          # 保持引用，避免连接被回收
    return registry


def test_expected_capabilities_registered():
    registry = make_registry()
    assert registry.names() == (
        "image.generate",
        "video.submit",
        "video.query",
        "media.fetch",
        "image_host.upload",
        "llm.chat",
        "vision.describe",
        "judge.ask",
        "rag.search",
        "web.search",
        "web.fetch_image",
    )


def test_kb_search_is_registered_only_with_a_knowledge_store(tmp_path):
    """知识库检索是本地能力：接了知识库才注册，没接就不该出现在能力表里。"""
    from app.services.knowledge import KnowledgeStore

    http = HttpClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), backoff=0
    )
    knowledge = KnowledgeStore(tmp_path / "h.db", root=tmp_path / "knowledge")
    try:
        registry = build_registry(http=http, credentials=make_credentials(), knowledge=knowledge)

        assert "kb.search" in registry.names()
        spec = registry.spec("kb.search")
        assert spec.params["required"] == ["requirement"]
        assert spec.has_side_effect(SIDE_DISK_READ)
    finally:
        knowledge.close()

    without = build_registry(http=http, credentials=make_credentials())
    assert "kb.search" not in without.names()


def test_platform_limits_live_in_metadata():
    """视频每分钟 1 个任务的平台限制必须写在能力里，供调度器与界面读取。"""
    registry = make_registry()
    limits = registry.rate_limits()
    assert limits == {"video.submit": {"per_minute": 1}}
    assert registry.spec("video.submit").side_effects >= {SIDE_NETWORK, SIDE_PAID}


def test_web_tools_metadata_and_budget():
    """联网两项能力：搜索会花钱、配图会落盘，预算在闸门里各有一份。"""
    from app.capabilities.middleware import DEFAULT_BUDGET_LIMITS

    registry = make_registry()
    search = registry.spec("web.search")
    assert search.params["required"] == ["query"]
    assert search.side_effects >= {SIDE_NETWORK, SIDE_PAID}
    image = registry.spec("web.fetch_image")
    assert image.params["required"] == ["url"]
    assert image.side_effects >= {SIDE_NETWORK, SIDE_DISK_WRITE}
    assert DEFAULT_BUDGET_LIMITS["web.search"] == 3
    assert DEFAULT_BUDGET_LIMITS["web.fetch_image"] == 4


async def test_web_search_without_key_returns_reason_not_error():
    """没配 key 时不能抛错中断流程：返回 ok=false + 一句能显示给用户的 reason。"""
    registry = make_registry()
    result = await registry.invoke("web.search", {"query": "国潮海报"})

    assert result["ok"] is False
    assert "SEARCH_API_KEY" in result["reason"]
    assert result["items"] == []


async def test_web_search_goes_through_client(tmp_path):
    import json as _json

    from app.clients.search_client import SearchClient
    from app.config.credentials import SearchCredentials

    def handler(request: httpx.Request) -> httpx.Response:
        assert _json.loads(request.content)["query"] == "国潮海报"
        return httpx.Response(
            200,
            json={"results": [{"title": "配色指南", "url": "https://a.example/1", "content": "红金克制"}]},
        )

    http = HttpClient(transport=httpx.MockTransport(handler), backoff=0)
    search = SearchClient(http, SearchCredentials(provider="tavily", api_key="sk-s"))
    registry = build_registry(http=http, credentials=make_credentials(), search=search)

    result = await registry.invoke("web.search", {"query": "国潮海报"})
    assert result["ok"] is True and result["count"] == 1
    assert result["items"][0]["snippet"] == "红金克制"


async def test_web_fetch_image_writes_file(tmp_path):
    from app.clients.search_client import SearchClient
    from app.config.credentials import SearchCredentials

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=png)), backoff=0)
    search = SearchClient(http, SearchCredentials(api_key="sk-s"), image_dir=tmp_path / "web_refs")
    registry = build_registry(http=http, credentials=make_credentials(), search=search)

    result = await registry.invoke("web.fetch_image", {"url": "https://img.example/a.png"})
    assert result["path"].endswith(".png")
    assert (tmp_path / "web_refs").exists()


def test_image_tool_declares_limits_and_enums():
    spec = make_registry().spec("image.generate")
    props = spec.params["properties"]
    assert props["images"]["maxItems"] == 5
    assert props["size"]["enum"] == list(IMAGE_SIZES)
    assert props["model"]["enum"] == list(IMAGE_MODELS)
    assert spec.params["required"] == ["prompt"]
    assert spec.timeout_s == 120


def test_video_tool_declares_public_url_requirement_and_enums():
    props = make_registry().spec("video.submit").params["properties"]
    assert props["images"]["items"]["format"] == "uri"
    assert props["images"]["maxItems"] == 5
    assert props["seconds"]["enum"] == list(SECONDS_OPTIONS)
    assert props["aspect_ratio"]["enum"] == list(ASPECT_RATIOS)
    assert props["model"]["enum"] == list(VIDEO_MODELS)


def test_upload_tool_is_flagged_as_upload_side_effect():
    """上传会把本地文件送到公网，未来必须走人工审批队列。"""
    spec = make_registry().spec("image_host.upload")
    assert spec.has_side_effect(SIDE_UPLOAD)
    assert not spec.idempotent
    assert spec.params["properties"]["provider"]["enum"] == ["auto", "github", "see"]


def test_mcp_shape_export():
    tools = {t["name"]: t for t in make_registry().mcp_tools()}
    assert set(tools) == set(make_registry().names())
    for tool in tools.values():
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["inputSchema"]["type"] == "object"


def test_duplicate_registration_rejected():
    registry = ToolRegistry()
    spec = ToolSpec(name="x", description="d", params={"type": "object"})
    registry.register(spec, lambda params: None)
    with pytest.raises(ValueError):
        registry.register(spec, lambda params: None)


async def test_unknown_capability_raises():
    registry = make_registry()
    with pytest.raises(AppError):
        await registry.invoke("nope")
    with pytest.raises(AppError):
        registry.spec("nope")


async def test_invoke_generate_image_goes_through_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://cdn/out.png"}]})

    registry = make_registry(handler)
    url = await registry.invoke("image.generate", {"prompt": "一只猫", "size": "512x512"})
    assert url == "https://cdn/out.png"


async def test_invoke_llm_without_config_gives_clear_error():
    registry = make_registry()
    with pytest.raises(AppError) as excinfo:
        await registry.invoke("llm.chat", {"messages": [{"role": "user", "content": "hi"}]})
    assert "LLM_API_KEY" in str(excinfo.value)


def test_vision_and_judge_metadata():
    """两个新能力都联网、都花钱、都可重放；评估超时给足（实测原图要 66 秒）。"""
    registry = make_registry()
    vision = registry.spec("vision.describe")
    judge = registry.spec("judge.ask")

    assert vision.side_effects >= {SIDE_NETWORK, SIDE_PAID}
    assert vision.idempotent is True and vision.timeout_s >= 120
    assert "320" in vision.description          # 图片尺寸的约定写进说明，避免后人又发原图
    assert "judge.ask" in vision.description

    assert judge.side_effects >= {SIDE_NETWORK, SIDE_PAID}
    assert judge.params["required"] == ["state", "questions"]
    assert judge.timeout_s <= 60


async def test_invoke_vision_describe_goes_through_client(tmp_path):
    import base64

    from PIL import Image

    image_path = tmp_path / "a.png"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(image_path)
    payload = {"choices": [{"message": {"content": '{"subject": "测试图", "flaws": []}'}}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    registry = make_registry(handler)
    result = await registry.invoke("vision.describe", {"image": str(image_path)})

    assert result["subject"] == "测试图"
    assert result["model"]
    assert base64.b64encode(b"") == b""      # 保持导入被使用


async def test_invoke_judge_ask_returns_typed_answers():
    payload = {
        "model": "jev-1.13.0",
        "answers": {"fits": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    registry = make_registry(lambda r: httpx.Response(200, json=payload))
    result = await registry.invoke(
        "judge.ask",
        {"state": {"a": 1}, "questions": {"fits": {"type": "noul", "instructions": "q"}}},
    )

    assert result["answers"]["fits"]["noul"] == 0.9
    assert result["model"] == "jev-1.13.0"
