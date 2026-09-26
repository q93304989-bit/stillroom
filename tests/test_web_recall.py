"""混合检索（第三期）：本地优先、不够才联网，阈值三层取最高，配图默认关。

这一层的验收标准来自方案第四、六节，逐条对应：

    · 本地命中 ≥ 阈值 → 用本地的，不联网（省额度）
    · 本地命中 < 阈值 → 自动联网补齐
    · 用户明确要求联网 → 本地够多也联网
    · 阈值 = max(用户设置, 模型判断)  ← 模型不能把用户设的下限压低
    · 联网配图默认关，关着时**连候选图都不请求**

草稿上必须写清「为什么联网 / 为什么没联网」，所以每个用例都断言 `decide` 与 reason。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.agent.runtime import AgentRuntime
from app.bootstrap import build_context
from app.config import settings

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 48


class Responder:
    """假 Jev：理解阶段按需回答 reference_need / web_search。"""

    def __init__(self, *, reference_need: str = "0", web_search: float = 0.0, task: str = "image") -> None:
        self.reference_need = reference_need
        self.web_search = web_search
        self.task = task

    def answers(self, questions: dict) -> dict:
        out: dict = {}
        if "task" in questions:
            out["task"] = {"type": "choice", "choice": self.task, "confidence": 0.9}
        if "feasible" in questions:
            out["feasible"] = {"type": "noul", "noul": 1.0}
        if "enough" in questions:
            out["enough"] = {"type": "noul", "noul": 0.9}
        if "aspect" in questions:
            out["aspect"] = {"type": "choice", "choice": "16:9", "confidence": 0.9}
        if "reference_need" in questions:
            out["reference_need"] = {
                "type": "choice", "choice": self.reference_need, "confidence": 0.8
            }
        if "web_search" in questions:
            out["web_search"] = {"type": "noul", "noul": self.web_search}
        return out


def make_agent(tmp_path, responder: Responder, *, search_key: str = "sk-search", provider: str = "tavily"):
    """造一套能跑通「理解 + 找参考」的运行时；搜索与配图都走 MockTransport。"""
    http_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        http_calls.append(url)
        if url.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn/out.png"}]})
        if url == "https://cdn/out.png":
            return httpx.Response(200, content=base64.b64decode("iVBORw0KGgo="))
        if "/chat/completions" in url:
            return httpx.Response(200, json={"choices": [{"message": {"content": "提示词"}}]})
        if "/v1/systemone" in url:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": responder.answers(body.get("questions") or {}),
                    "usage": {},
                },
            )
        if url.startswith("https://api.tavily.com/search"):
            http_calls.append(f"tavily-body:{request.content.decode('utf-8')}")
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "国潮配色指南", "url": "https://a.example/1", "content": "红金为主，压住饱和"},
                        {"title": "留白与对称", "url": "https://a.example/2", "content": "居中对称，四周留白"},
                    ],
                    "images": ["https://img.example/ok.png", "https://img.example/bad.png"],
                },
            )
        if url == "https://img.example/ok.png":
            return httpx.Response(200, content=PNG_BYTES)
        if url == "https://img.example/bad.png":
            return httpx.Response(200, content=b"<html>not an image</html>")
        return httpx.Response(404, json={"message": f"no route {url}"})

    env_file = tmp_path / ".env"
    lines = [
        "AGNES_API_KEY=sk-agnes",
        "AGNES_BASE_URL=https://api.test/v1",
        "DEEPSEEK_API_KEY=sk-ds",
        "TYPESAFE_API_KEY=apikey-ts",
    ]
    if search_key:
        lines += [f"SEARCH_PROVIDER={provider}", f"SEARCH_API_KEY={search_key}"]
    elif provider == "off":
        lines.append("SEARCH_PROVIDER=off")
    # 没有 key 也不写 provider：走「没配 key」那条分支（默认 provider 是 tavily）
    env_file.write_text("\n".join(lines), encoding="utf-8")

    context = build_context(
        env_file=env_file, data_dir=tmp_path / "data", transport=httpx.MockTransport(handler)
    )
    runtime = AgentRuntime(
        registry=context.registry, generation=context.generation, bus=context.bus, history=context.history
    )
    tool_calls: list[tuple[str, dict]] = []
    original = context.registry.invoke

    async def spy(name, params=None, *, context=None):
        tool_calls.append((name, dict(params or {})))
        return await original(name, params, context=context)

    context.registry.invoke = spy
    return runtime, context, tool_calls, http_calls


def add_kb(context, tmp_path, text: str, name: str = "国潮风格说明.md") -> None:
    """往知识库里放一份能命中的资料（本地命中的来源）。"""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    doc = context.knowledge.add(path)
    context.knowledge.build(doc.id)


def tools_of(tool_calls: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in tool_calls]


def web_items(draft: dict) -> list[dict]:
    return [item for item in draft["items"] if item["kind"] == "web"]


def tavily_body(http_calls: list[str]) -> str:
    return next((call[len("tavily-body:"):] for call in http_calls if call.startswith("tavily-body:")), "")


# --------------------------------------------------------------------------- 本地够不够

async def test_local_enough_skips_web(tmp_path):
    """本地命中够阈值就不联网——省的是用户的钱，这条必须真的不发请求。"""
    runtime, context, tool_calls, http_calls = make_agent(tmp_path, Responder())
    add_kb(context, tmp_path, "# 配色\n\n国潮海报配色以红金为主，留白要够。\n\n# 构图\n\n居中对称，主体偏上。\n\n# 气质\n\n克制，不要满。")
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色 留白 对称")

    assert "web.search" not in tools_of(tool_calls)
    assert not [call for call in http_calls if "tavily" in call]
    assert draft["decide"]["web"] is False
    assert "没联网" in draft["decide"]["reason"]
    assert web_items(draft) == []
    context.history.close()


async def test_empty_local_triggers_web(tmp_path):
    runtime, context, tool_calls, _ = make_agent(tmp_path, Responder())
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色")

    assert "web.search" in tools_of(tool_calls)
    assert draft["decide"]["web"] is True
    assert "自动联网补齐" in draft["decide"]["reason"]
    assert len(web_items(draft)) == 2
    assert web_items(draft)[0]["origin"].startswith("外部线索")
    context.history.close()


async def test_one_local_hit_is_still_below_threshold(tmp_path):
    """本地只有 1 条、阈值 2：照样联网。阈值是「下限」，不是「有就行」。"""
    runtime, context, tool_calls, _ = make_agent(tmp_path, Responder())
    add_kb(context, tmp_path, "# 配色\n\n国潮海报配色以红金为主。")
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色")

    assert draft["decide"]["local_hits"] == 1
    assert "web.search" in tools_of(tool_calls)
    assert "本地只有 1 条" in draft["decide"]["reason"]
    context.history.close()


async def test_user_threshold_zero_means_never_search_for_more(tmp_path):
    """把阈值设成 0 = 「本地只要命中就不再联网」，这是用户能选的省额度档位。"""
    runtime, context, tool_calls, _ = make_agent(tmp_path, Responder())
    settings.update(min_local_refs=0)

    draft = await runtime.draft("国潮 海报 配色")

    assert "web.search" not in tools_of(tool_calls)
    assert draft["decide"]["threshold"] == 0
    context.history.close()


# --------------------------------------------------------------------------- 阈值三层

async def test_llm_can_raise_the_threshold(tmp_path):
    """模型判「要 3 条以上」时，本地只有 1 条 → 按 3 算，联网。"""
    runtime, context, tool_calls, _ = make_agent(tmp_path, Responder(reference_need="3+"))
    add_kb(context, tmp_path, "# 配色\n\n国潮海报配色以红金为主。")
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色")

    assert draft["decide"]["reference_need"] == 3
    assert draft["decide"]["threshold"] == 3            # max(2, 3)
    assert "web.search" in tools_of(tool_calls)
    context.history.close()


async def test_llm_cannot_lower_the_user_threshold(tmp_path):
    """模型判 0 也压不低用户设的 5——这条是「取 max」的意义所在。"""
    runtime, context, tool_calls, _ = make_agent(tmp_path, Responder(reference_need="0"))
    add_kb(context, tmp_path, "# 配色\n\n国潮海报配色以红金为主。")
    settings.update(min_local_refs=5)

    draft = await runtime.draft("国潮 海报 配色")

    assert draft["decide"]["threshold"] == 5
    assert "web.search" in tools_of(tool_calls)
    context.history.close()


async def test_explicit_user_request_searches_even_when_local_is_enough(tmp_path):
    """用户点名要联网：本地够多也搜（判「是」的依据是意图识别那一问）。"""
    runtime, context, tool_calls, _ = make_agent(tmp_path, Responder(web_search=1.0))
    add_kb(context, tmp_path, "# 配色\n\n国潮海报配色以红金为主，留白要够。\n\n# 构图\n\n居中对称。\n\n# 气质\n\n克制。")
    settings.update(min_local_refs=1)

    draft = await runtime.draft("国潮 海报 配色 留白 对称")

    assert "web.search" in tools_of(tool_calls)
    assert "你点名要联网" in draft["decide"]["reason"]
    context.history.close()


async def test_web_source_off_is_never_searched(tmp_path):
    runtime, context, tool_calls, http_calls = make_agent(tmp_path, Responder())
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色", sources={"web": False})

    assert "web.search" not in tools_of(tool_calls)
    assert not [call for call in http_calls if "tavily" in call]
    assert draft["decide"]["web"] is False
    assert "联网线索已关闭" in draft["decide"]["reason"]
    context.history.close()


# --------------------------------------------------------------------------- 搜不了要说清

async def test_missing_key_is_explained_not_hidden(tmp_path):
    runtime, context, tool_calls, http_calls = make_agent(tmp_path, Responder(), search_key="")
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色")

    assert draft["status"] == "ok"                     # 联网没配好不能拖垮整条流程
    assert draft["decide"]["web"] is False
    assert "SEARCH_API_KEY" in draft["decide"]["reason"]
    assert not [call for call in http_calls if "tavily" in call]
    assert "web.search" in tools_of(tool_calls)        # 调了，但客户端立刻回了「搜不了」
    context.history.close()


async def test_provider_off_is_explained(tmp_path):
    runtime, context, _, _ = make_agent(tmp_path, Responder(), provider="off")
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色")

    assert draft["decide"]["web"] is False
    assert "off" in draft["decide"]["reason"]
    context.history.close()


# --------------------------------------------------------------------------- 联网配图

async def test_web_images_off_by_default_downloads_nothing(tmp_path):
    """默认关：不下载，而且**连候选图都不请求**（方案 6.2）。"""
    runtime, context, tool_calls, http_calls = make_agent(tmp_path, Responder())
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色")

    assert "web.fetch_image" not in tools_of(tool_calls)
    assert not [item for item in draft["items"] if item["kind"] == "web_image"]
    assert "include_images" not in tavily_body(http_calls)
    assert draft["decide"]["web_images"] is False
    context.history.close()


async def test_web_images_on_downloads_and_skips_broken(tmp_path):
    """开着时：能下的下下来（kind=web_image，记原图 URL），坏的那张跳过。"""
    runtime, context, tool_calls, http_calls = make_agent(tmp_path, Responder())
    settings.update(min_local_refs=2)

    draft = await runtime.draft("国潮 海报 配色", sources={"web_images": True})

    assert "include_images" in tavily_body(http_calls)
    assert tools_of(tool_calls).count("web.fetch_image") == 2      # 两张候选都试了
    images = [item for item in draft["items"] if item["kind"] == "web_image"]
    assert len(images) == 1                                        # 坏的那张被拒收
    assert images[0]["ref"].endswith(".png")
    assert "img.example/ok.png" in images[0]["meta"]["url"]
    assert draft["decide"]["web_images"] is True
    assert "免责声明" in draft["decide"]["reason"]
    context.history.close()

