"""六步流水线：阶段顺序、判断分支、降级与闸门。全部走 MockTransport，不联网。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from app.agent.runtime import AgentRuntime
from app.bootstrap import build_context
from app.services.history import Record
from app.state.jobs import Job, JobStatus

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class Responder:
    """按问题 id 回答的假 Jev；evaluate 阶段的分数按轮次取值。"""

    def __init__(self, **kwargs) -> None:
        self.enough = kwargs.get("enough", 0.9)
        self.task = kwargs.get("task", kwargs.get("kind", "image"))   # 判断链第一步：任务类型
        self.feasible = kwargs.get("feasible", 1.0)                   # 第二步：可行性
        self.kind = kwargs.get("kind", "image")
        self.aspect = kwargs.get("aspect", "9:16")
        self.fits = kwargs.get("fits", 0.9)
        self.quality = list(kwargs.get("quality", [3.0]))
        self.confidence = list(kwargs.get("confidence", [0.8]))
        self.fix = list(kwargs.get("fix", ["none"]))
        self.evaluate_calls = 0

    def answers(self, questions: dict) -> dict:
        def take(values, index):
            return values[min(index, len(values) - 1)]

        out: dict = {}
        if "task" in questions:
            out["task"] = {"type": "choice", "choice": self.task, "confidence": 0.9}
        if "feasible" in questions:
            out["feasible"] = {"type": "noul", "noul": self.feasible}
        if "enough" in questions:
            out["enough"] = {"type": "noul", "noul": self.enough}
        if "aspect" in questions:
            out["aspect"] = {"type": "choice", "choice": self.aspect, "confidence": 0.9}
        if "fits" in questions:
            index = self.evaluate_calls
            out["fits"] = {"type": "noul", "noul": self.fits}
            out["quality"] = {
                "type": "score",
                "score": take(self.quality, index),
                "confidence": take(self.confidence, index),
            }
            out["fix"] = {"type": "choice", "choice": take(self.fix, index), "confidence": 0.7}
            self.evaluate_calls += 1
        return out


def make_runtime(tmp_path, responder: Responder, *, vision_ok: bool = True, image_delay: float = 0.0):
    calls: list[str] = []

    async def chat_reply(text: str = "改写后的提示词：中秋明月下的庭院，暖色灯笼，竖构图"):
        return text

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(f"{request.method} {url.split('?')[0]}")
        if url.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn/out.png"}]})
        if url == "https://cdn/out.png":
            return httpx.Response(200, content=base64.b64decode(PNG))
        if "/chat/completions" in url:
            if not vision_ok:
                return httpx.Response(401, json={"message": "Invalid token"})
            body = json.loads(request.content)
            content = body["messages"][0]["content"]
            if isinstance(content, list):          # 视觉：多模态数组
                return httpx.Response(
                    200,
                    json={
                        "model": "deepseek-v4-flash-vision-exp",
                        "choices": [{"message": {"content": json.dumps({
                            "subject": "中秋庭院",
                            "style": "国潮插画",
                            "composition": "竖构图居中",
                            "lighting": "暖色灯笼",
                            "flaws": ["文字略糊"],
                        }, ensure_ascii=False)}}],
                    },
                )
            return httpx.Response(          # 文本：生成模型的提示词改写
                200,
                json={"choices": [{"message": {"content": "改写后的提示词：中秋明月下的庭院，暖色灯笼，竖构图"}}]},
            )
        if "/v1/systemone" in url:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": responder.answers(body.get("questions") or {}),
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            )
        return httpx.Response(404, json={"message": f"no route {url}"})

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AGNES_API_KEY=sk-agnes",
                "AGNES_BASE_URL=https://api.test/v1",
                "DEEPSEEK_API_KEY=sk-ds",
                "TYPESAFE_API_KEY=apikey-ts",
            ]
        ),
        encoding="utf-8",
    )
    context = build_context(
        env_file=env_file,
        data_dir=tmp_path / "data",
        transport=httpx.MockTransport(handler),
    )
    runtime = AgentRuntime(
        registry=context.registry,
        generation=context.generation,
        bus=context.bus,
        history=context.history,
        max_refine_rounds=1,
    )
    return runtime, context, calls


async def run_and_collect(runtime, context, request: str, **kwargs):
    phases: list[str] = []
    context.bus.subscribe(
        lambda event: phases.append(str(event.get("phase"))) if event.type == "agent.phase" else None
    )
    handle = runtime.start(request, **kwargs)
    job = await handle.wait()
    return job.result, phases, context


# --------------------------------------------------------------------------- 正常路径

async def test_happy_path_runs_six_phases_in_order(tmp_path):
    runtime, context, calls = make_runtime(tmp_path, Responder(quality=[3.0]))
    result, phases, ctx = await run_and_collect(runtime, context, "做一张中秋海报，竖版")

    assert result["status"] == "succeeded"
    assert phases == ["understand", "prepare", "generate", "evaluate", "deliver"]
    assert result["result_url"] == "https://cdn/out.png"
    assert result["attempts"] == 1
    assert result["evaluation"]["decision"] == "accept"
    assert result["description"]["subject"] == "中秋庭院"

    # 每一步的工具调用都发生过
    assert any("systemone" in call for call in calls)          # Jev
    assert any("/images/generations" in call for call in calls)  # 出图
    assert any("/chat/completions" in call for call in calls)    # 视觉 + 写提示词

    # 交付阶段把判断写进了历史记录的 meta
    record = ctx.history.list()[0]
    assert record.meta["agent"]["run_id"] == result["run_id"]
    assert record.meta["agent"]["evaluation"]["decision"] == "accept"
    ctx.history.close()


async def test_history_records_the_run_chain(tmp_path):
    """出图任务挂在这次运行之下（父子结构），这是将来回放的基础。"""
    runtime, context, _ = make_runtime(tmp_path, Responder())
    result, _, ctx = await run_and_collect(runtime, context, "随便来一张")

    record = ctx.history.list()[0]
    assert record.job_id
    run = runtime.run_of(result["run_id"])
    assert run is not None and run.parent_job_id is None      # 运行本身没有父
    ctx.history.close()


# --------------------------------------------------------------------------- 分支

async def test_ambiguous_request_asks_before_generating(tmp_path):
    runtime, context, calls = make_runtime(tmp_path, Responder(enough=0.2))
    result, phases, ctx = await run_and_collect(runtime, context, "来点好看的")

    assert result["status"] == "needs_input"
    assert result["message"]
    # 说清「我判的是哪一句、判了多少分」，用户才知道怎么补（不然再点一次画面一模一样）
    assert "充分度 0.20" in result["message"]
    assert "来点好看的" in result["message"]
    assert "直接开跑" in result["message"]                     # 也告诉他还有出口
    assert phases == ["understand"]                            # 没往下走
    assert not any("/images/generations" in call for call in calls)
    ctx.history.close()


async def test_force_skips_the_question_and_runs(tmp_path):
    """「不补了，直接开跑」：只跳过这一道追问，后面照常跑完。"""
    runtime, context, calls = make_runtime(tmp_path, Responder(enough=0.1))
    result, phases, ctx = await run_and_collect(runtime, context, "来点好看的", force=True)

    assert result["status"] == "succeeded"
    assert "generate" in phases
    ctx.history.close()


async def test_out_of_scope_request_is_refused_not_generated(tmp_path):
    """「帮我想个剧本」这类：说清我做不了，而不是编条画面提示词硬跑。"""
    runtime, context, calls = make_runtime(tmp_path, Responder(task="text"))
    result, phases, ctx = await run_and_collect(runtime, context, "先帮我想一个牛逼的剧本")

    assert result["status"] == "out_of_scope"
    assert "做不了" in result["message"]
    assert "文字产物" in result["message"]                      # 类型判成「写文字」，话要说到点上
    assert "先帮我想一个牛逼的剧本" in result["message"]        # 把用户原话摆出来，别让他猜
    assert not any("/images/generations" in call for call in calls)
    assert not any("/videos" in call for call in calls)
    ctx.history.close()


async def test_out_of_scope_can_still_be_forced(tmp_path):
    """用户说「不，我就是要出图」：跳过能力边界判断，照跑。"""
    runtime, context, calls = make_runtime(tmp_path, Responder(task="text"))
    result, phases, ctx = await run_and_collect(
        runtime, context, "先帮我想一个牛逼的剧本", force=True
    )

    assert result["status"] == "succeeded"
    assert any("/images/generations" in call for call in calls)
    ctx.history.close()


async def test_infeasible_media_request_is_refused(tmp_path):
    """类型是出图，但模型判「这条要求做不到」：同样拦下来，别硬跑。"""
    runtime, context, calls = make_runtime(tmp_path, Responder(task="image", feasible=0.1))
    result, phases, ctx = await run_and_collect(runtime, context, "做一张 8K 能直接印刷的海报")

    assert result["status"] == "out_of_scope"
    assert "做不了" in result["message"]
    assert not any("/images/generations" in call for call in calls)
    ctx.history.close()


async def test_video_reference_rejected_by_platform_retries_without_it(tmp_path):
    """参考图被平台拒了就退回纯文本再跑一次，别让一次本来能成的出片废掉。

    实测形态：历史里那条视频记录的 mp4 被当参考图，平台回 400「素材 URL 无法下载或是不支持
    的媒体格式」，用户看到的是「生成失败」。

    注意结尾的期望是 `needs_confirmation` 而不是 `succeeded`：视频**没有自动评估**
    （视觉模型只吃图片），所以交付前一定停在「请你自己看一眼」。
    注：这条用例早先能断言 succeeded，是因为它的 visual mock 对 mp4 也返回了成功描述——
    比真实接口宽容，从而掩盖了「视频其实评估不了」这个事实。
    """
    import asyncio as _asyncio

    from app.services.history import Record
    from app.state.jobs import JobHandle

    runtime, context, calls = make_runtime(tmp_path, Responder(kind="video"))
    # 造一条图片素材：视频的参考图要从图片记录里来（公网地址）
    context.history.add(Record(
        kind="image", status="success", prompt="海面日落 长镜头",
        result_url="https://cdn/ref.png",
    ))
    seen: list[tuple[str, ...]] = []

    def fake_start_video(request, *, context=None, parent_job_id=None):
        # 与真实接口一致：同步返回句柄（异步的是句柄里的任务）
        seen.append(tuple(request.images))
        job = Job(tool="video.submit")
        if request.images:
            job.status = JobStatus.FAILED
            job.error = "请求被拒绝（HTTP 400）：素材 URL 无法下载或是不支持的媒体格式"
            job.error_kind = "http"
        else:
            job.status = JobStatus.SUCCEEDED
            job.result = {"url": "https://cdn/out.mp4", "media_path": None}
        return JobHandle(job, _asyncio.create_task(_asyncio.sleep(0)))

    context.generation.start_video = fake_start_video
    result, phases, ctx = await run_and_collect(runtime, context, "海边日落的长镜头视频")

    assert seen == [("https://cdn/ref.png",), ()], "第一次带参考图、第二次必须去掉"
    # 出片成功，但视频不能自动评估 → 交用户确认
    assert result["status"] == "needs_confirmation"
    assert result["result_url"] == "https://cdn/out.mp4"
    assert any("去掉参考图重试" in step.get("title", "") for step in result["steps"])
    assert any("视频暂不能自动评估" in step.get("detail", "") for step in result["steps"])
    ctx.history.close()


async def test_video_is_not_sent_to_the_vision_model(tmp_path):
    """出视频时**绝不能**把 mp4 发给视觉模型：那是注定失败的调用，白烧额度。

    为什么值得单独钉一条：改之前它会发出去（`_describe` 拿 URL 当图片），
    结果是「视觉模型不可用」这种误导性的降级原因，外加一次白花的请求。
    这条用例直接数「带图片的 chat 请求」有没有发生。
    """
    import asyncio as _asyncio

    from app.state.jobs import JobHandle

    runtime, context, calls = make_runtime(tmp_path, Responder(kind="video"))
    vision_calls: list[str] = []

    def fake_start_video(request, *, context=None, parent_job_id=None):
        job = Job(tool="video.submit")
        job.status = JobStatus.SUCCEEDED
        job.result = {"url": "https://cdn/out.mp4", "media_path": None}
        return JobHandle(job, _asyncio.create_task(_asyncio.sleep(0)))

    context.generation.start_video = fake_start_video

    # 包一层：记录每次 vision.describe 的入参
    original_invoke = context.registry.invoke

    async def spy(name, params=None, *, context=None):
        if name == "vision.describe":
            vision_calls.append(str((params or {}).get("image") or ""))
        return await original_invoke(name, params, context=context)

    context.registry.invoke = spy

    result, phases, ctx = await run_and_collect(runtime, context, "海边日落的长镜头视频")

    assert result["status"] == "needs_confirmation"
    assert result["result_url"] == "https://cdn/out.mp4"      # 片子出来了
    assert vision_calls == [], f"mp4 被发给视觉模型了：{vision_calls}"
    assert "视频暂不能自动评估" in " ".join(
        step.get("detail", "") for step in result["steps"]
    )
    ctx.history.close()


async def test_unexpected_error_becomes_a_visible_failure(tmp_path):
    """意料之外的异常（不是 AppError）也必须收成「失败 + 原因」，不能静默消失。

    这是实测踩到的坑：运行里冒出非 AppError，异常直接穿透，界面一直停在上一次的样子，
    用户看到的是「点了没反应」——而打包后的窗口程序没有控制台，连行报错都看不到。
    """
    from app.config import logs

    runtime, context, calls = make_runtime(tmp_path, Responder())
    original = context.registry.invoke

    async def boom(tool, params, **kwargs):
        if tool == "rag.search":
            raise ValueError("boom-意外错误")
        return await original(tool, params, **kwargs)

    context.registry.invoke = boom
    result, phases, ctx = await run_and_collect(runtime, context, "中秋海报，竖版")

    assert result["status"] == "failed"
    assert "ValueError" in result["message"] and "boom-意外错误" in result["message"]
    # 现场落在数据目录的日志里（打包后这是唯一能查到的东西）
    log_text = logs.error_log_path().read_text(encoding="utf-8")
    assert "agent.run" in log_text and "boom-意外错误" in log_text
    ctx.history.close()


async def test_low_quality_triggers_refine_then_accept(tmp_path):
    runtime, context, calls = make_runtime(
        tmp_path, Responder(quality=[1.0, 3.0], fix=["style", "none"])
    )
    result, phases, ctx = await run_and_collect(runtime, context, "中秋海报")

    assert result["status"] == "succeeded"
    assert result["attempts"] == 2
    assert "refine" in phases
    assert result["prompt"].startswith("改写后的提示词")        # 精修换了提示词
    ctx.history.close()


async def test_low_confidence_asks_user_instead_of_deciding(tmp_path):
    runtime, context, _ = make_runtime(tmp_path, Responder(quality=[2.5], confidence=[0.2]))
    result, phases, ctx = await run_and_collect(runtime, context, "中秋海报")

    assert result["status"] == "needs_confirmation"
    assert "确认" in result["message"]
    assert "deliver" not in phases
    ctx.history.close()


async def test_vision_unavailable_degrades_to_user_confirmation(tmp_path):
    runtime, context, _ = make_runtime(tmp_path, Responder(), vision_ok=False)
    result, phases, ctx = await run_and_collect(runtime, context, "中秋海报")

    assert result["status"] == "needs_confirmation", (
        f"{result['status']} / {result['message']} / 描述={result.get('description')}"
    )
    assert "视觉" in result["message"]
    assert result["result_url"]                                    # 图仍然出来了
    ctx.history.close()


async def test_budget_gate_stops_the_run(tmp_path):
    """预算被闸门挡住时，运行停下并如实说明，而不是继续烧额度。"""
    runtime, context, calls = make_runtime(tmp_path, Responder(quality=[1.0, 1.0]))
    run = Job(tool="agent.run", params={"request": "中秋海报"})
    run.context["usage"] = {"image.generate": 6}                   # 预先用满默认预算
    result = await runtime.run("中秋海报", run=run)

    assert result.status == "budget_exceeded"
    assert "额度" in result.message
    assert not any("/images/generations" in call for call in calls)
    context.history.close()


async def test_phase_scope_is_set_during_run(tmp_path):
    """每个阶段都会把工具白名单写进运行上下文（PhaseGate 据此拦截）。"""
    runtime, context, _ = make_runtime(tmp_path, Responder())
    seen: list[tuple[str, tuple]] = []
    context.bus.subscribe(
        lambda event: seen.append((str(event.get("phase")), ()))
        if event.type == "agent.phase"
        else None
    )
    handle = runtime.start("中秋海报")
    job = await handle.wait()
    run = runtime.run_of(job.id)

    assert job.status is JobStatus.SUCCEEDED
    assert [phase for phase, _ in seen] == ["understand", "prepare", "generate", "evaluate", "deliver"]
    assert run is not None
    assert "deliver" in seen[-1][0]
    assert run.context["allowed_tools"] == ()                      # 交付阶段没有可用工具
    context.history.close()


# --------------------------------------------------------------------------- 上下文草稿

def _touch_png(tmp_path, name: str = "ref.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return path


def _touch_text(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _spy_on_registry(context, calls: list[tuple[str, dict]]):
    """记下每次工具调用的名字与参数（断言「关掉的来源根本没被调用」靠它）。"""
    original = context.registry.invoke

    async def spy(tool, params, **kwargs):
        calls.append((tool, dict(params)))
        return await original(tool, params, **kwargs)

    context.registry.invoke = spy


async def test_draft_gathers_context_without_generating(tmp_path):
    """草稿只跑「理解 + 找参考」：给出可编辑的条目，但绝不生成。"""
    runtime, context, calls = make_runtime(tmp_path, Responder())
    context.history.add(Record(
        kind="image", status="success", prompt="中秋海报 竖版 国潮",
        media_path=str(_touch_png(tmp_path)), result_url="https://cdn/a.png",
    ))

    draft = await runtime.draft("中秋海报")

    assert draft["status"] == "ok"
    assert draft["kind"] == "image" and draft["aspect"] == "9:16"
    kinds = {item["kind"] for item in draft["items"]}
    assert kinds and kinds <= {"history", "history_text"}
    assert [step["title"] for step in draft["steps"]] == ["理解需求", "找参考"]
    assert not any("/images/generations" in call for call in calls), "草稿阶段不该出图"
    ctx_history = context.history
    assert ctx_history.count() == 1                    # 草稿不写运行记录（只有那条素材）
    context.history.close()


async def test_result_carries_the_context_it_actually_used(tmp_path):
    """跑完的结果要带上「这次实际用了什么」：自动模式靠它落快照（第四期）。"""
    runtime, context, calls = make_runtime(tmp_path, Responder())
    context.history.add(Record(
        kind="image", status="success", prompt="中秋海报 竖版 国潮",
        media_path=str(_touch_png(tmp_path)), result_url="https://cdn/a.png",
    ))

    result = await runtime.run("中秋海报")

    assert result.status == "succeeded"
    payload = result.context
    assert payload["requirement"]
    assert payload["kind"] == "image"
    assert payload["sources"]["history"] is True
    kinds = {item["kind"] for item in payload["items"]}
    assert kinds and kinds <= {"history", "history_text"}
    context.history.close()


async def test_auto_run_respects_saved_sources(tmp_path):
    """自动模式（没有草稿）也要遵守设置里存的来源开关，不能绕过去全查。"""
    from app.config import settings as settings_module

    runtime, context, calls = make_runtime(tmp_path, Responder())
    settings_module.update(context_sources={"history": False, "knowledge": False})
    try:
        result = await runtime.run("中秋海报")

        assert result.status == "succeeded"
        assert not any("rag.search" in str(call) for call in calls), "关了历史却还是查了历史"
        assert result.context["sources"]["history"] is False
    finally:
        settings_module.update(context_sources={})
        context.history.close()


async def test_confirmed_context_is_what_runs(tmp_path):
    """用户确认的上下文就是运行的输入：删掉的不复现、补的进提示词、参考图照用。"""
    runtime, context, http_calls = make_runtime(tmp_path, Responder())
    png = _touch_png(tmp_path)
    tool_calls: list[tuple[str, dict]] = []
    _spy_on_registry(context, tool_calls)

    confirmed = {
        "requirement": "中秋海报",
        "kind": "image",
        "aspect": "9:16",
        "items": [
            {"kind": "history", "ref": str(png), "origin": "历史作品", "user_state": "kept"},
            {"kind": "history_text", "ref": "被删掉的旧线索", "user_state": "removed"},
        ],
        "notes": "必须留白，别放文字",
        "sources": {"history": True},
    }
    # 注意：这里的第二个参数就是「上下文草稿」，别和 AppContext 混了
    handle = runtime.start("中秋海报", context=confirmed)
    job = await handle.wait()
    result = job.result

    assert result["status"] == "succeeded"
    tools = [tool for tool, _ in tool_calls]
    assert "rag.search" not in tools, "确认过的上下文不该再检索一遍"
    # 参考图真的传给了生成
    image_call = next(params for tool, params in tool_calls if tool == "image.generate")
    assert tuple(image_call["images"]) == (str(png),)
    # 用户删掉的线索没进提示词，补的那句进去了
    composer = next(
        params for tool, params in tool_calls
        if tool == "llm.chat" and "messages" in params
    )
    text = str(composer["messages"])
    assert "被删掉的旧线索" not in text
    assert "必须留白" in text
    assert "（你删掉了 1 条）" in str(result["steps"])
    context.history.close()


async def test_switched_off_source_is_not_searched_at_all(tmp_path):
    """关掉「历史参考」就不调 rag.search——是根本不检索，不是查完再藏起来。"""
    runtime, context, http_calls = make_runtime(tmp_path, Responder())
    tool_calls: list[tuple[str, dict]] = []
    _spy_on_registry(context, tool_calls)

    draft = await runtime.draft("中秋海报", sources={"history": False})

    assert draft["status"] == "ok"
    assert draft["items"] == []
    assert draft["decide"]["history"] is False
    assert "rag.search" not in [tool for tool, _ in tool_calls]
    assert "历史参考已关闭" in draft["steps"][-1]["detail"]
    context.history.close()


async def test_knowledge_source_flows_into_the_draft(tmp_path):
    """知识库开着时，命中的片段要进草稿：条目 kind=kb，来源写清是哪份文件第几片。"""
    runtime, context, _ = make_runtime(tmp_path, Responder())
    doc = context.knowledge.add(_touch_text(
        tmp_path, "国潮风格说明.md",
        "# 配色\n\n国潮海报的配色以红金为主，饱和度要压低，留白要足够。",
    ))
    context.knowledge.build(doc.id)

    draft = await runtime.draft("国潮 海报 配色")

    kb_items = [item for item in draft["items"] if item["kind"] == "kb"]
    assert kb_items, "知识库命中的片段没进上下文"
    assert "国潮风格说明.md" in kb_items[0]["origin"]
    assert "第 1 片" in kb_items[0]["origin"]
    assert context.history.count() == 0                 # 草稿仍然不写运行记录
    context.history.close()


async def test_switched_off_knowledge_source_is_not_searched(tmp_path):
    """关掉知识库就不调 kb.search——与历史参考同一套「根本不检索」。"""
    runtime, context, _ = make_runtime(tmp_path, Responder())
    tool_calls: list[tuple[str, dict]] = []
    _spy_on_registry(context, tool_calls)
    doc = context.knowledge.add(_touch_text(
        tmp_path, "国潮风格说明.md",
        "# 配色\n\n国潮海报的配色以红金为主，饱和度要压低，留白要足够。",
    ))
    context.knowledge.build(doc.id)

    draft = await runtime.draft("国潮 海报 配色", sources={"knowledge": False})

    assert "kb.search" not in [tool for tool, _ in tool_calls]
    assert not [item for item in draft["items"] if item["kind"] == "kb"]
    context.history.close()
