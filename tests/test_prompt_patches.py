"""补丁生效链路与设置页桥：接受了补丁，运行时真的用它；桥的三个动作走通。不联网。"""

from __future__ import annotations

import io
import json
import time

import httpx
import pytest
from PIL import Image

from app.bootstrap import build_context
from app.ui.async_runner import AsyncRunner
from app.ui.selfupdate_bridge import SelfUpdateBridge
from app.services.history import Record

PATCH = {
    "composer_suffix": "光线描述必须具体（光源、色温、方向）",
    "aspect_preference": "3:4",
    "question_overrides": {"evaluate.fits": "这张图达没达到需求的要求？"},
}


def tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (24, 18), (30, 40, 80)).save(buffer, "PNG")
    return buffer.getvalue()


def wait_until(app, predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def make_world(tmp_path, *, aspect_answer: str | None = "16:9", seed: int = 0):
    """装配 + 假模型。返回 (context, captured)，captured 记录 LLM 与判断的请求体。"""
    captured: dict[str, list] = {"chat": [], "judge": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn.test/out.png"}]})
        if url == "https://cdn.test/out.png":
            return httpx.Response(200, content=tiny_png())
        if "/chat/completions" in url:
            body = json.loads(request.content)
            content = body["messages"][0]["content"]
            if isinstance(content, list):          # 视觉描述
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                    "subject": "中秋庭院", "style": "国潮插画",
                    "composition": "竖构图居中", "lighting": "暖色灯笼", "flaws": [],
                }, ensure_ascii=False)}}]})
            captured["chat"].append(content)       # 写提示词 / 精修
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "改写后的提示词，竖构图"}}]}
            )
        if "/v1/systemone" in url:
            body = json.loads(request.content)
            captured["judge"].append(body.get("questions") or {})
            questions = body.get("questions") or {}
            out: dict = {}
            if "enough" in questions:
                out["enough"] = {"type": "noul", "noul": 0.9}
                out["kind"] = {"type": "choice", "choice": "image", "confidence": 0.9}
                out["aspect"] = {"type": "choice", "choice": aspect_answer, "confidence": 0.9}
            if "fits" in questions:
                out["fits"] = {"type": "noul", "noul": 0.9}
                out["quality"] = {"type": "score", "score": 3.0, "confidence": 0.8}
                out["fix"] = {"type": "choice", "choice": "none", "confidence": 0.7}
            return httpx.Response(200, json={
                "model": "jev-1.13.0", "answers": out,
                "usage": {"input_tokens": 100, "output_tokens": 20},
            })
        return httpx.Response(404, json={"message": "no route"})

    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGNES_API_KEY=sk-agnes\n"
        "AGNES_BASE_URL=https://api.test/v1\n"
        "DEEPSEEK_API_KEY=sk-ds\n"
        "TYPESAFE_API_KEY=apikey-ts\n",
        encoding="utf-8",
    )
    context = build_context(
        env_file=env_file,
        data_dir=tmp_path / "data",
        transport=httpx.MockTransport(handler),
    )
    for index in range(seed):
        record = context.history.add(Record(
            kind="image",
            prompt=f"海报 {index}",
            meta={"agent": {"run_id": f"run-{index}", "requirement": f"海报 {index}",
                            "evaluation": {"fits": 0.8, "quality": 2.0, "fix": "lighting"}}},
        ))
        context.history.set_feedback(record.id, action="accept")
    return context, captured


# --------------------------------------------------------------------------- 生效链路

async def test_accepted_patch_reaches_the_composer(tmp_path):
    context, captured = make_world(tmp_path)
    row = context.prompts.propose(PATCH, reason="测试")
    context.prompts.accept(row["id"])

    handle = context.agent.start("中秋海报，竖版")
    job = await handle.wait()

    assert job.result["status"] == "succeeded"
    assert captured["chat"], "写提示词没有发出请求"
    assert all("光线描述必须具体" in body for body in captured["chat"])
    assert all("3:4 画幅" in body for body in captured["chat"])


async def test_question_override_reaches_the_judge(tmp_path):
    context, captured = make_world(tmp_path)
    row = context.prompts.propose(PATCH, reason="测试")
    context.prompts.accept(row["id"])

    handle = context.agent.start("中秋海报，竖版")
    await handle.wait()

    fit_questions = [
        q["fits"]["instructions"] for q in captured["judge"] if "fits" in q
    ]
    assert fit_questions, "评估阶段没有发问"
    assert all("达没达到需求的要求" in text for text in fit_questions)


async def test_new_questions_are_also_overridable(tmp_path):
    """第三期新加的「要几条参考 / 要不要联网」两问同样受补丁管辖。

    这两问直接决定花不花搜索额度，所以它们的问法必须和别的问法一样能改——
    否则「意图识别不准」时用户只能干看着，改不了。
    """
    context, captured = make_world(tmp_path)
    row = context.prompts.propose(
        {"question_overrides": {
            "understand.reference_need": "做这条要几条参考才够？",
            "understand.web_search": "用户是不是点名要联网查？",
        }},
        reason="测试",
    )
    context.prompts.accept(row["id"])

    draft = await context.agent.draft("中秋海报，竖版")

    need = [q["reference_need"]["instructions"] for q in captured["judge"] if "reference_need" in q]
    web = [q["web_search"]["instructions"] for q in captured["judge"] if "web_search" in q]
    assert need and all("几条参考" in text for text in need)
    assert web and all("点名要联网" in text for text in web)
    assert draft["status"] == "ok"


async def test_aspect_preference_is_the_fallback(tmp_path):
    # 模型给不出画幅（choice=None）时，用补丁里的偏好而不是默认 16:9
    context, _captured = make_world(tmp_path, aspect_answer=None)
    row = context.prompts.propose({"aspect_preference": "3:4"}, reason="测试")
    context.prompts.accept(row["id"])

    handle = context.agent.start("中秋海报")
    job = await handle.wait()

    understand = next(s for s in job.result["steps"] if s["phase"] == "understand")
    assert understand["data"]["aspect"] == "3:4"


async def test_without_patch_behavior_is_unchanged(tmp_path):
    context, captured = make_world(tmp_path)       # 没有任何补丁
    handle = context.agent.start("中秋海报，竖版")
    await handle.wait()
    assert all("光线描述必须具体" not in body for body in captured["chat"])


# --------------------------------------------------------------------------- 设置页桥

async def test_bridge_generate_accept_reject(qt_app, tmp_path):
    context, _captured = make_world(tmp_path, seed=10)
    runner = AsyncRunner()
    bridge = SelfUpdateBridge(context, runner)
    try:
        assert bridge.sampleCount == 10
        assert bridge.hasPending is False

        bridge.generateSuggestion()
        # 假 LLM 会 404（make_world 没配建议端点）→ 不会出 pending；直接登记一条来测动作
        assert wait_until(qt_app, lambda: not bridge.busy)

        row = context.prompts.propose(PATCH, reason="光线问题反复出现",
                                      evidence={"samples": 10})
        bridge.refresh()
        assert bridge.hasPending is True
        assert "光线问题反复出现" in bridge.pendingText

        bridge.acceptPending()
        assert bridge.currentVersion == 1
        assert "光线描述必须具体" in bridge.currentText
        assert bridge.canRollback is False

        # 再来一条拒绝掉：留痕且不影响当前版本
        context.prompts.propose({"composer_suffix": "另一条"}, reason="")
        bridge.refresh()
        bridge.rejectPending()
        assert bridge.hasPending is False
        assert bridge.currentVersion == 1
        statuses = [item["status"] for item in bridge.versions()]
        assert "rejected" in statuses and "active" in statuses
    finally:
        runner.close()
