"""助手页后端：实时步骤、四种请示、打断、结果操作。全部走 MockTransport，不联网。"""

from __future__ import annotations

import io
import json
import time

import httpx
import pytest
from PIL import Image

from app.bootstrap import build_context
from app.config import settings
from app.services.history import Record
from app.state.events import Event
from app.ui.agent_bridge import AgentBridge
from app.ui.async_runner import AsyncRunner
from app.ui.bridge import UiBridge
from app.ui.theme import Theme

CDN = "https://cdn.test/out.png"


def tiny_png() -> bytes:
    """一张真能解码的小图：评估阶段会把本地缓存缩到 320px，假字节过不了这一步。"""
    buffer = io.BytesIO()
    Image.new("RGB", (24, 18), (30, 40, 80)).save(buffer, "PNG")
    return buffer.getvalue()


PNG_BYTES = tiny_png()


def wait_until(app, predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


class Responder:
    """按问题 id 回答的假 Jev；`enough` / `quality` / `confidence` 可传列表表示第几轮取第几个。"""

    def __init__(self, **kwargs) -> None:
        def as_list(value, default):
            if value is None:
                return [default]
            return list(value) if isinstance(value, (list, tuple)) else [value]

        self.enough = as_list(kwargs.get("enough"), 0.9)
        self.fits = as_list(kwargs.get("fits"), 0.9)
        self.quality = as_list(kwargs.get("quality"), 3.0)
        self.confidence = as_list(kwargs.get("confidence"), 0.8)
        self.fix = as_list(kwargs.get("fix"), "none")
        self.kind = kwargs.get("kind", "image")
        self.task = kwargs.get("task", self.kind)       # 判断链第一步：任务类型
        self.feasible = kwargs.get("feasible", 1.0)     # 第二步：可行性
        self.aspect = kwargs.get("aspect", "16:9")
        self.understand_calls = 0
        self.evaluate_calls = 0

    @staticmethod
    def _take(values, index):
        return values[min(index, len(values) - 1)]

    def answers(self, questions: dict) -> dict:
        out: dict = {}
        if "enough" in questions:
            out["enough"] = {"type": "noul", "noul": self._take(self.enough, self.understand_calls)}
            out["task"] = {"type": "choice", "choice": self.task, "confidence": 0.9}
            out["feasible"] = {"type": "noul", "noul": self.feasible}
            out["aspect"] = {"type": "choice", "choice": self.aspect, "confidence": 0.9}
            self.understand_calls += 1
        if "fits" in questions:
            index = self.evaluate_calls
            out["fits"] = {"type": "noul", "noul": self._take(self.fits, index)}
            out["quality"] = {
                "type": "score",
                "score": self._take(self.quality, index),
                "confidence": self._take(self.confidence, index),
            }
            out["fix"] = {"type": "choice", "choice": self._take(self.fix, index), "confidence": 0.7}
            self.evaluate_calls += 1
        return out


class AgentHarness:
    """把装配、两条桥拼起来（界面层不实例化 QML，所以这些用例跑得很快）。"""

    def __init__(self, tmp_path, responder: Responder | None = None) -> None:
        self.responder = responder or Responder()
        self.calls: list[str] = []
        self.vision_sources: list[str] = []      # 每次视觉请求用的图（data: 缩图还是远端 URL）
        self.cdn_bytes = PNG_BYTES               # 可换成坏字节，模拟「下载被截断」
        self.steps: list[tuple] = []
        self.replaced: list = []
        self.finished: list[str] = []
        self.notices: list[tuple[str, str]] = []

        env_file = tmp_path / ".env"
        env_file.write_text(
            "AGNES_API_KEY=sk-agnes\n"
            "AGNES_BASE_URL=https://api.test/v1\n"
            "DEEPSEEK_API_KEY=sk-ds\n"
            "TYPESAFE_API_KEY=apikey-ts\n",
            encoding="utf-8",
        )
        self.context = build_context(
            env_file=env_file,
            data_dir=tmp_path / "data",
            transport=httpx.MockTransport(self._handle),
        )
        self.runner = AsyncRunner()
        self.theme = Theme(dark=True)
        self.bridge = UiBridge(self.context, self.runner, self.theme)
        self.agent = AgentBridge(self.context, self.runner, self.bridge)
        # 这些用例测的是「跑起来之后」的行为；草稿模式（先看后跑）另有专门用例
        self.agent.setContextMode("auto")

        self.agent.stepAdded.connect(lambda *args: self.steps.append(args))
        self.agent.stepsReplaced.connect(lambda items: self.replaced.append(list(items)))
        self.agent.runFinished.connect(lambda run_id, status: self.finished.append(status))
        self.agent.noticeRaised.connect(lambda level, text: self.notices.append((level, text)))

    # ---------------------------------------------------------------- 假的网络

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(f"{request.method} {url.split('?')[0]}")
        if url.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": CDN}]})
        if url == CDN:
            return httpx.Response(200, content=self.cdn_bytes)
        if "/chat/completions" in url:
            body = json.loads(request.content)
            content = body["messages"][0]["content"]
            if isinstance(content, list):        # 视觉：多模态数组
                self.vision_sources.append(str(content[1]["image_url"]["url"])[:24])
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": json.dumps({
                        "subject": "中秋庭院",
                        "style": "国潮插画",
                        "composition": "竖构图居中",
                        "lighting": "暖色灯笼",
                        "flaws": [],
                    }, ensure_ascii=False)}}]},
                )
            return httpx.Response(               # 文本：写提示词
                200, json={"choices": [{"message": {"content": "改写后的提示词：中秋明月，暖色灯笼，竖构图"}}]}
            )
        if "/v1/systemone" in url:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": self.responder.answers(body.get("questions") or {}),
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            )
        if request.method == "POST":              # 视频提交
            return httpx.Response(200, json={"video_id": "vid-bridge"})
        if request.method == "GET":               # 视频轮询
            return httpx.Response(200, json={"status": "queued", "progress": 0})
        return httpx.Response(404, json={"message": f"no route {url}"})

    # ---------------------------------------------------------------- 便捷

    def run_and_wait(self, app, request: str, *, timeout: float = 10.0) -> bool:
        expected = len(self.finished) + 1        # 要在起跑前记数：跑完再看就晚了一步
        self.agent.run(request)
        if not wait_until(app, lambda: not self.agent.running, timeout=timeout):
            return False
        # 信号是排队投递给主线程的：状态变了不等于 runFinished 已经送达
        return wait_until(app, lambda: len(self.finished) >= expected, timeout=timeout)

    def step_titles(self) -> list[str]:
        return [title for _phase, title, *_rest in self.steps]

    @property
    def final_steps(self) -> list:
        return self.replaced[-1] if self.replaced else []

    def close(self) -> None:
        self.agent.detach()
        self.bridge.detach()
        try:
            self.runner.run_blocking(self.context.http.aclose(), timeout=5.0)
        except Exception:                    # pragma: no cover - 已经断开时不必纠缠
            pass
        assert self.runner.close(), "asyncio 线程没能在超时内退出"
        self.context.history.close()
        self.context.prompts.close()


@pytest.fixture
def agent_ui(qt_app, tmp_path):
    harness = AgentHarness(tmp_path)
    yield harness
    harness.close()


# --------------------------------------------------------------------------- 正常路径

def test_run_shows_live_steps_then_full_steps(qt_app, agent_ui):
    assert agent_ui.run_and_wait(qt_app, "中秋海报，竖版")

    assert agent_ui.agent.status == "succeeded", (
        agent_ui.agent.status + " / " + agent_ui.agent.message
        + " / " + agent_ui.agent.decisionText
    )
    assert agent_ui.agent.hasResult
    assert agent_ui.agent.resultUrl == CDN
    assert agent_ui.agent.resultLocalPath.endswith(".png")
    assert agent_ui.finished == ["succeeded"]

    # 实时那几条：按阶段标题出现（理解需求 / 找参考 / 生成 / 评估 / 交付）
    titles = agent_ui.step_titles()
    assert titles[:3] == ["理解需求", "找参考", "生成"]
    assert "交付" in titles

    # 结束后换成带耗时与结论的完整步骤
    final = agent_ui.final_steps
    phases = [step["phase"] for step in final]
    assert [p for index, p in enumerate(phases) if index == 0 or phases[index - 1] != p] == [
        "understand", "prepare", "generate", "evaluate", "deliver",
    ]
    assert all("seconds" in step for step in final)
    assert agent_ui.agent.stepCount == len(final)
    assert any(step["phase"] == "prepare" and step["title"] == "写提示词" for step in final)

    # 调用统计（花在哪了，界面直接展示）
    assert "出图 1 次" in agent_ui.agent.usageText
    assert "判断" in agent_ui.agent.usageText

    # 判断结论一句话能读懂
    assert "达标" in agent_ui.agent.decisionText

    # 评估用的是本地缓存缩图（就是实测里 4.6 秒那条路径），而不是让视觉模型去远端拉原图
    assert agent_ui.vision_sources
    assert all(src.startswith("data:image/jpeg") for src in agent_ui.vision_sources)


def test_unreadable_cache_falls_back_to_the_remote_url(qt_app, tmp_path):
    """本地缓存读不出来时退回远端地址再试一次，而不是整轮评估直接作废。"""
    harness = AgentHarness(tmp_path)
    try:
        harness.cdn_bytes = b"\x89PNG\r\n\x1a\ntruncated"     # 落盘了，但不是能解码的图
        assert harness.run_and_wait(qt_app, "中秋海报")

        assert harness.agent.status == "succeeded", harness.agent.message
        assert harness.vision_sources == [CDN[:24]]           # 第二次用的是远端地址
        assert harness.agent.decisionText
    finally:
        harness.close()


def test_run_records_history_with_agent_meta(qt_app, agent_ui):
    assert agent_ui.run_and_wait(qt_app, "中秋海报")

    assert wait_until(qt_app, lambda: bool(agent_ui.context.history.list()))
    record = agent_ui.context.history.list()[0]
    assert record.prompt
    assert record.meta["agent"]["run_id"] == agent_ui.agent.runId
    assert record.media_path


def test_second_run_is_refused_while_running(qt_app, agent_ui):
    agent_ui.agent.run("第一句")
    agent_ui.agent.run("第二句")

    assert any(level == "warn" for level, _ in agent_ui.notices)
    assert wait_until(qt_app, lambda: not agent_ui.agent.running)


# --------------------------------------------------------------------------- 四种请示

def test_needs_input_then_supplement_reruns(qt_app, tmp_path):
    harness = AgentHarness(tmp_path, Responder(enough=[0.2, 0.9]))
    try:
        assert harness.run_and_wait(qt_app, "来点好看的")
        assert harness.agent.status == "needs_input"
        assert harness.agent.pendingAction == "input"
        assert harness.agent.needsUser
        assert "具体" in harness.agent.message
        assert not harness.agent.hasResult
        assert not any("/images/generations" in call for call in harness.calls)

        # 补充一句 → 第二次运行（判断这次说信息够了）
        harness.agent.continueWith("竖版海报，国潮插画风格")
        assert wait_until(qt_app, lambda: not harness.agent.running)

        assert harness.agent.status == "succeeded"
        assert harness.agent.requirement == "来点好看的。竖版海报，国潮插画风格"
        assert wait_until(qt_app, lambda: harness.finished == ["needs_input", "succeeded"])
    finally:
        harness.close()


def test_run_anyway_skips_the_question(qt_app, tmp_path):
    """「不补了，直接开跑」：判断模型说不够，用户自己清楚够用，也得能往下走。"""
    harness = AgentHarness(tmp_path, Responder(enough=0.2))
    try:
        assert harness.run_and_wait(qt_app, "来点好看的")
        assert harness.agent.status == "needs_input"

        harness.agent.runAnyway()
        assert wait_until(qt_app, lambda: harness.agent.status == "succeeded", timeout=20)
        assert harness.agent.hasResult
    finally:
        harness.close()


def test_wait_error_becomes_a_visible_failure(qt_app, tmp_path):
    """运行抛意外异常时，界面必须给出「失败 + 原因」，不能停在上一次的样子。"""
    harness = AgentHarness(tmp_path)

    class BoomHandle:
        id = "run-boom"
        done = False

        def cancel(self) -> None:                    # pragma: no cover - 本用例不打断
            pass

        async def wait(self):
            raise RuntimeError("连接被重置")

    harness.context.agent.start = lambda *args, **kwargs: BoomHandle()
    try:
        harness.agent.run("随便来点什么")
        assert wait_until(qt_app, lambda: harness.agent.status == "failed")
        assert "连接被重置" in harness.agent.message
        # 注意：status 是直接读的属性，而提示是跨线程信号送到界面线程的——两者不是
        # 同一时刻到达。这里等提示自己到（不等就可能偶发地「看到失败却还没看到提示」）。
        assert wait_until(qt_app, lambda: any(level == "error" for level, _ in harness.notices))
    finally:
        harness.close()


def test_low_confidence_asks_user_then_accept_marks_feedback(qt_app, tmp_path):
    harness = AgentHarness(tmp_path, Responder(confidence=[0.2]))
    try:
        assert harness.run_and_wait(qt_app, "中秋海报")
        assert harness.agent.status == "needs_confirmation"
        assert harness.agent.pendingAction == "confirm"
        assert harness.agent.hasResult               # 图已经出来了，只是判断不确定
        assert harness.agent.message               # 说清了为什么问

        harness.agent.acceptResult()
        assert harness.agent.status == "accepted"
        assert wait_until(
            qt_app,
            lambda: bool(harness.context.history.list())
            and harness.context.history.list()[0].last_action == "accept",
        )
        record = harness.context.history.list()[0]
        assert record.last_action == "accept"        # 下次找参考会优先带上它
    finally:
        harness.close()


def test_unsatisfied_confirm_reruns_with_a_new_direction(qt_app, tmp_path):
    harness = AgentHarness(tmp_path, Responder(confidence=[0.2, 0.9]))
    try:
        assert harness.run_and_wait(qt_app, "中秋海报")
        assert harness.agent.status == "needs_confirmation"

        harness.agent.continueWith("")
        assert wait_until(qt_app, lambda: not harness.agent.running)

        assert harness.agent.status == "succeeded"
        assert "换个明显不同的方向" in harness.agent.requirement
    finally:
        harness.close()


# --------------------------------------------------------------------------- 打断与结果操作

def test_cancel_stops_a_running_job(qt_app, tmp_path):
    harness = AgentHarness(tmp_path, Responder(kind="video"))
    try:
        harness.agent.run("做一段海浪的视频")
        assert wait_until(qt_app, lambda: harness.agent.running)
        harness.agent.cancel()
        assert wait_until(qt_app, lambda: harness.agent.status == "canceled")
        assert wait_until(qt_app, lambda: harness.finished[-1:] == ["canceled"])
    finally:
        harness.close()


def test_interrupt_right_after_start_is_not_dropped(qt_app, tmp_path):
    """点完「开始」立刻打断：那时运行句柄还没登记，这一下也必须算数。

    与 `UiBridge.cancel` 同一个坑：界面线程直接判定「没有在跑的运行」会把请求吞掉。
    """
    harness = AgentHarness(tmp_path, Responder(kind="video"))
    try:
        harness.agent.run("做一段海浪的视频")
        harness.agent.cancel()                    # 故意不等它登记句柄
        assert wait_until(qt_app, lambda: harness.agent.status == "canceled")
        assert wait_until(qt_app, lambda: harness.finished[-1:] == ["canceled"])
    finally:
        harness.close()


def test_load_into_generator_reuses_the_prompt(qt_app, agent_ui):
    loaded: list[tuple[str, dict]] = []
    agent_ui.bridge.paramsLoaded.connect(lambda kind, params: loaded.append((kind, params)))

    assert agent_ui.run_and_wait(qt_app, "中秋海报")
    agent_ui.agent.loadIntoGenerator()

    assert loaded, "没有把提示词送回生成页"
    kind, params = loaded[-1]
    assert kind == "image"
    assert params["prompt"] == agent_ui.agent.prompt
    assert isinstance(params["images"], list)


def test_video_references_are_filtered_to_public_urls(qt_app, agent_ui):
    """视频接口只吃公网 URL：本地路径不能在「载入到生成页」时被带进去。"""
    loaded: list[tuple[str, dict]] = []
    agent_ui.bridge.paramsLoaded.connect(lambda kind, params: loaded.append((kind, params)))
    assert agent_ui.run_and_wait(qt_app, "中秋海报")

    agent_ui.agent._view.kind = "video"
    agent_ui.agent._view.references = ["C:/local/a.png", "https://cdn/b.png"]
    agent_ui.agent.loadIntoGenerator()

    assert loaded[-1] == ("video", {"prompt": agent_ui.agent.prompt, "images": ["https://cdn/b.png"]})


def test_clear_resets_everything(qt_app, agent_ui):
    assert agent_ui.run_and_wait(qt_app, "中秋海报")
    agent_ui.agent.clear()

    assert agent_ui.agent.status == "idle"
    assert agent_ui.agent.stepCount == 0
    assert not agent_ui.agent.hasResult


# --------------------------------------------------------------------------- 事件过滤

def test_events_from_other_runs_are_ignored(agent_ui):
    """只认本次运行的事件：别的运行（或别处）的阶段事件不该画到这条时间线上。"""
    before = len(agent_ui.steps)
    agent_ui.agent._run_id = "job-current"

    agent_ui.context.bus.emit(Event(type="agent.phase", job_id="job-other", payload={"phase": "generate"}))
    agent_ui.context.bus.emit(Event(type="job.progress", job_id="job-current", payload={}))

    assert len(agent_ui.steps) == before


# --------------------------------------------------------------------------- 上下文（草稿模式）

def seed_reference(harness, tmp_path, name: str = "ref.png") -> str:
    """往历史里放一条「可当参考图」的作品，返回它的本地路径。"""
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    harness.context.history.add(Record(
        kind="image", status="success", prompt="中秋海报 竖版 国潮",
        media_path=str(path), result_url="https://cdn/ref.png",
    ))
    return str(path)


def test_draft_mode_shows_context_before_running(qt_app, tmp_path):
    """草稿模式（默认）：点「开始」先出上下文，不生成；确认后才真跑。"""
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("draft")
    ref = seed_reference(harness, tmp_path)
    try:
        harness.agent.run("中秋海报，竖版")

        assert wait_until(qt_app, lambda: harness.agent.status == "draft")
        assert harness.agent.hasContext
        assert harness.agent.contextEditable
        items = harness.agent.contextItems
        assert items and any(item["kind"] == "history" for item in items)
        assert all(not item["removed"] for item in items)
        assert not any("/images/generations" in call for call in harness.calls)   # 还没生成

        harness.agent.confirmContext()
        assert wait_until(qt_app, lambda: harness.agent.status == "succeeded", timeout=20)

        record = harness.context.history.list()[0]
        assert ref in record.refs                       # 参考图真的用上了
        assert harness.calls.count("POST https://api.test/v1/images/generations") == 1
    finally:
        harness.close()


def test_removed_context_item_is_not_used(qt_app, tmp_path):
    """删掉的那条参考：运行时不复活，也不进生成请求。"""
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("draft")
    ref = seed_reference(harness, tmp_path)
    try:
        harness.agent.run("中秋海报，竖版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")

        key = next(item["key"] for item in harness.agent.contextItems if item["kind"] == "history")
        harness.agent.removeContextItem(key)
        assert harness.agent.contextRemoved == 1
        assert any(item["removed"] for item in harness.agent.contextItems)

        harness.agent.confirmContext()
        assert wait_until(qt_app, lambda: harness.agent.status == "succeeded", timeout=20)

        record = harness.context.history.list()[0]
        assert ref not in record.refs, "用户删掉的参考又进了生成请求"
    finally:
        harness.close()


def test_notes_added_in_draft_reach_the_prompt(qt_app, tmp_path):
    """「我补一句」要进提示词。"""
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("draft")
    try:
        harness.agent.run("中秋海报，竖版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")

        harness.agent.setContextNotes("不要出现任何文字")
        assert harness.agent.contextNotes == "不要出现任何文字"
        harness.agent.confirmContext()
        assert wait_until(qt_app, lambda: harness.agent.status == "succeeded", timeout=20)

        assert "不要出现任何文字" in harness.agent.requirement
    finally:
        harness.close()


def test_auto_mode_runs_in_one_step(qt_app, tmp_path):
    """自动模式：点「开始」直接跑，不经过草稿。"""
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("auto")
    try:
        assert harness.agent.contextMode == "auto"
        harness.agent.run("中秋海报，竖版")
        assert harness.agent.status in ("running", "succeeded")
        assert wait_until(qt_app, lambda: harness.agent.status == "succeeded", timeout=20)
        assert not harness.agent.hasContext
    finally:
        harness.close()


def test_discarded_draft_leaves_no_record(qt_app, tmp_path):
    """草稿被丢掉时不留运行记录（也没出图）。"""
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("draft")
    seed_reference(harness, tmp_path)
    before = harness.context.history.count()
    try:
        harness.agent.run("中秋海报，竖版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")

        harness.agent.discardContext()
        assert harness.agent.status == "idle"
        assert not harness.agent.hasContext
        assert harness.context.history.count() == before
        assert not any("/images/generations" in call for call in harness.calls)
    finally:
        harness.close()


def test_draft_sources_can_be_remembered_as_default(qt_app, tmp_path):
    """草稿卡上的「记住为默认」：这次临时改的开关，下次新草稿按它来。"""
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("draft")
    seed_reference(harness, tmp_path)
    try:
        harness.agent.run("中秋海报，竖版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")

        harness.agent.toggleContextSource("web_images", True)
        harness.agent.toggleContextSource("knowledge", False)
        harness.agent.rememberContextSources()

        stored = settings.load()["context_sources"]
        assert stored["web_images"] is True
        assert stored["knowledge"] is False

        # 下一份草稿按记住的来（不是沿用上一次草稿里的临时值）
        harness.agent.discardContext()
        harness.agent.run("再来一张，横版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")
        assert harness.agent.contextSources["knowledge"] is False
        assert harness.agent.contextSources["web_images"] is True
    finally:
        harness.close()


# --------------------------------------------------------------------------- 第四期：长期档案 / 改参考再跑一次

def test_run_finished_offers_edit_context_and_reopens_a_copy(qt_app, tmp_path):
    """自动模式跑完也能「改参考再跑一次」：快照是复制出来的，改它不影响原运行。"""
    harness = AgentHarness(tmp_path)
    ref = seed_reference(harness, tmp_path)
    try:
        assert harness.run_and_wait(qt_app, "中秋海报，竖版")
        assert harness.agent.canEditContext, "跑完之后没有可改的上下文快照"
        run_id = harness.agent.runId

        harness.agent.editContext()

        assert harness.agent.status == "draft"
        assert harness.agent.hasContext
        assert harness.agent.contextEditable
        keys = [item["key"] for item in harness.agent.contextItems]
        assert any(item["kind"] == "history" for item in harness.agent.contextItems)

        # 删掉一条再确认：运行用的就是改过的这份，而原快照留着当那次运行的留痕
        history_key = next(
            item["key"] for item in harness.agent.contextItems if item["kind"] == "history"
        )
        harness.agent.removeContextItem(history_key)
        harness.agent.confirmContext()
        assert wait_until(qt_app, lambda: harness.agent.status == "succeeded", timeout=20)

        snapshot = harness.context.contexts.of_run(run_id)
        assert snapshot is not None and snapshot.run_id == run_id
        assert [item.key for item in snapshot.items if item.user_state == "kept"]
        assert harness.context.history.list()[0].refs == []      # 删掉的那条没进这次生成
    finally:
        harness.close()


def test_saved_profile_can_be_applied_to_a_new_draft(qt_app, tmp_path):
    """长期档案：存下来，下一次新草稿点「套用」就带上来（偏好能沉淀复用）。"""
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("draft")
    try:
        harness.agent.run("中秋海报，竖版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")
        harness.agent.setContextNotes("不要出现任何文字")
        harness.agent.addContextText("必须留白，别放字")
        harness.agent.saveProfile("国潮海报", "不要出现任何文字", False)

        assert [profile["name"] for profile in harness.agent.profiles] == ["国潮海报"]
        assert harness.agent.hasProfiles
        # 没设成默认：新草稿不会自动带，要点「套用」才上（默认与非默认行为要分开验）

        harness.agent.discardContext()
        harness.agent.run("换一张，横版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")
        assert not any(
            "必须留白" in item["title"] for item in harness.agent.contextItems
        ), "非默认档案不该自己套上来"

        harness.agent.applyProfile(harness.agent.profiles[0]["id"])
        titles = [item["title"] for item in harness.agent.contextItems]
        assert any("必须留白" in title for title in titles), "档案里的条目没有套上来"
        assert "不要出现任何文字" in harness.agent.contextNotes
    finally:
        harness.close()


def test_default_profile_is_applied_to_every_new_draft(qt_app, tmp_path):
    """设为默认的档案：之后每份新草稿自动带上，不用手动点「套用」。"""
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("draft")
    try:
        harness.agent.run("中秋海报，竖版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")
        harness.agent.addContextText("每次都要留白")
        harness.agent.saveProfile("默认风格", "", True)
        assert harness.agent.profiles[0]["isDefault"] is True

        harness.agent.discardContext()
        harness.agent.run("换一张，横版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")
        titles = [item["title"] for item in harness.agent.contextItems]
        assert any("每次都要留白" in title for title in titles), "默认档案没有自动套上"
    finally:
        harness.close()


def test_deleting_a_profile_leaves_runs_untouched(qt_app, tmp_path):
    harness = AgentHarness(tmp_path)
    harness.agent.setContextMode("draft")
    try:
        harness.agent.run("中秋海报，竖版")
        assert wait_until(qt_app, lambda: harness.agent.status == "draft")
        harness.agent.addContextText("要求 A", "A")
        harness.agent.saveProfile("会被删掉的档案", "", False)
        profile_id = harness.agent.profiles[0]["id"]

        harness.agent.deleteProfile(profile_id)

        assert harness.agent.profiles == []
        assert harness.agent.hasProfiles is False
        assert harness.agent.hasContext is True      # 草稿还在，删档案不该动它
    finally:
        harness.close()
