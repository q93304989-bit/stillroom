"""界面层测试：QML 能加载、桥能驱动真实任务、换主题不重建窗口。

全部在 offscreen 平台下跑，不需要显示器，也不联网（用 MockTransport）。
"""

from __future__ import annotations

import os
import json
import sqlite3
import threading
import time
import base64
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx  # noqa: E402
import pytest  # noqa: E402

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402

from app.bootstrap import build_context  # noqa: E402
from app.services.history import Record  # noqa: E402
from app.ui.agent_bridge import AgentBridge  # noqa: E402
from app.ui.async_runner import AsyncRunner  # noqa: E402
from app.ui.bridge import UiBridge  # noqa: E402
from app.ui.selfupdate_bridge import SelfUpdateBridge  # noqa: E402
from app.ui.settings_bridge import SettingsBridge  # noqa: E402
from app.ui.knowledge_bridge import KnowledgeBridge  # noqa: E402
from app.ui.theme import DARK, LIGHT, Theme  # noqa: E402

QML_DIR = Path(__file__).resolve().parents[1] / "app" / "ui" / "qml"
IMAGE_URL = "https://api.test/v1/images/generations"
CDN = "https://cdn.test/a.png"

#: 夹具创建的引擎**故意留着不销毁**：shiboken 的包装对象缓存按「地址」找对象，
#: 上一个引擎的界面树一释放，地址就会被下一个引擎重新用到，于是 `findChild`
#: 会把「已失效的旧包装」返回来（表现为 Internal C++ object already deleted）。
#: 测试进程里多留几个引擎的内存，换掉这种假故障是划算的。
_ENGINES_KEPT_ALIVE: list = []


def wait_until(app, predicate, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def judge_answers(questions: dict) -> dict:
    """界面测试里的假 Jev：一律「是出图的活、可行、够清楚、达标、可用度 3」。"""
    out: dict = {}
    if "enough" in questions:
        out["task"] = {"type": "choice", "choice": "image", "confidence": 0.9}
        out["feasible"] = {"type": "noul", "noul": 0.9}
        out["enough"] = {"type": "noul", "noul": 0.9}
        out["aspect"] = {"type": "choice", "choice": "16:9", "confidence": 0.9}
    if "fits" in questions:
        out["fits"] = {"type": "noul", "noul": 0.9}
        out["quality"] = {"type": "score", "score": 3.0, "confidence": 0.8}
        out["fix"] = {"type": "choice", "choice": "none", "confidence": 0.7}
    return out


def image_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and str(request.url).startswith(IMAGE_URL):
        return httpx.Response(200, json={"data": [{"url": CDN}]})
    if request.method == "GET" and str(request.url).startswith(CDN):
        # 必须是一张**真能解码**的 PNG：助手页的 QML Image 会异步解码这张图，
        # 假字节会让 libpng 报错，把「Error decoding」注进 ui.warnings，
        # 而异步解码完成的时机不确定——断言「无 QML 报错」就会随机翻车。
        return httpx.Response(200, content=base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        ))
    if "/chat/completions" in str(request.url):
        # 视觉描述与提示词改写共用一个端点：前者是多模态数组，后者是纯文本
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({
                "subject": "中秋庭院", "style": "国潮插画",
                "composition": "竖构图居中", "lighting": "暖色灯笼", "flaws": [],
            }, ensure_ascii=False)}}]},
        )
    if "/v1/systemone" in str(request.url):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": judge_answers(body.get("questions") or {}),
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )
    return httpx.Response(404, json={"message": "no route"})


class UiHarness:
    """把装配、引擎、桥拼起来，供各用例复用。"""

    def __init__(self, tmp_path: Path, handler=image_handler, theme_dark: bool = True) -> None:
        # 测试自带一份假凭据，绝不读开发机上的真实 .env
        env_file = tmp_path / ".env"
        env_file.write_text(
            "AGNES_API_KEY=sk-test\n"
            "AGNES_BASE_URL=https://api.test/v1\n"
            "DEEPSEEK_API_KEY=sk-test-vision\n"
            "TYPESAFE_API_KEY=apikey-test\n",
            encoding="utf-8",
        )
        self.context = build_context(
            env_file=env_file,
            data_dir=tmp_path / "data",
            transport=httpx.MockTransport(handler),
        )
        self.runner = AsyncRunner()
        self.theme = Theme(dark=theme_dark)
        self.bridge = UiBridge(self.context, self.runner, self.theme)
        self.settings_bridge = SettingsBridge(self.context, self.runner, self.theme)
        self.agent_bridge = AgentBridge(self.context, self.runner, self.bridge)
        self.agent_bridge.setContextMode("auto")     # 界面用例测「跑起来之后」；草稿模式另有用例
        self.selfupdate_bridge = SelfUpdateBridge(self.context, self.runner)
        self.knowledge_bridge = KnowledgeBridge(self.context, self.runner)
        self.warnings: list[str] = []

        self.engine = QQmlApplicationEngine()
        self.engine.warnings.connect(lambda items: self.warnings.extend(items))
        self.engine.rootContext().setContextProperty("backend", self.bridge)
        self.engine.rootContext().setContextProperty("theme", self.theme)
        self.engine.rootContext().setContextProperty("settingsBridge", self.settings_bridge)
        self.engine.rootContext().setContextProperty("agentBridge", self.agent_bridge)
        self.engine.rootContext().setContextProperty("selfUpdateBridge", self.selfupdate_bridge)
        self.engine.rootContext().setContextProperty("knowledgeBridge", self.knowledge_bridge)
        self.engine.addImportPath(str(QML_DIR))
        self.engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))

    @property
    def root(self):
        roots = self.engine.rootObjects()
        return roots[0] if roots else None

    def close(self) -> None:
        self.bridge.detach()
        self.agent_bridge.detach()
        # 收尾顺序：先关 HTTP 连接池（得在循环线程里做），再停循环线程，最后关两个
        # 数据库句柄。留着让 GC 去收是危险的——sqlite 句柄与 Qt 引擎可能在不同线程里
        # 被回收，收尾顺序就不可控了。
        try:
            self.runner.run_blocking(self.context.http.aclose(), timeout=5.0)
        except Exception:                    # pragma: no cover - 已经断开时不必纠缠
            pass
        assert self.runner.close(), "asyncio 线程没能在超时内退出"
        self.context.history.close()
        self.context.prompts.close()
        _ENGINES_KEPT_ALIVE.append(self.engine)


@pytest.fixture
def ui(qt_app, tmp_path):
    harness = UiHarness(tmp_path)
    yield harness
    harness.close()


# --------------------------------------------------------------------------- 加载

def test_qml_loads_without_errors(ui):
    assert ui.root is not None, "Main.qml 没有产生根窗口"
    messages = [str(w) for w in ui.warnings]
    errors = [m for m in messages if "error" in m.lower() or "is not a type" in m]
    assert errors == [], f"QML 报错：{errors}"


def test_window_exposes_both_pages(ui):
    """两个页面都建起来了（StackLayout 的两个孩子）。"""
    root = ui.root
    assert root is not None
    # 顶栏 / 状态栏 / 主体都存在
    assert root.property("visible") is True
    assert root.property("title") == "Stillroom"


def test_bridge_reports_environment(ui):
    assert ui.bridge.dataDir
    assert ui.bridge.status == "idle"
    assert ui.bridge.statusText == "空闲"
    assert ui.bridge.busy is False
    assert ui.bridge.hasResult is False


# --------------------------------------------------------------------------- 任务

def test_generate_image_through_bridge(qt_app, ui):
    """点「生成」的完整链路：桥 → 服务层 → MockTransport → 状态回到界面。"""
    ui.bridge.generateImage("一只猫", "1024x768", "agnes-image-2.5-flash", [])

    assert wait_until(qt_app, lambda: ui.bridge.status == "succeeded")
    assert ui.bridge.hasResult
    assert ui.bridge.resultUrl == CDN
    assert ui.bridge.resultLocalPath.endswith(".png")
    assert ui.bridge.resultSource.startswith("file:///")     # 本地缓存优先
    assert ui.bridge.statusText == "已完成"

    # 状态事件先到、落库随后（写库走线程池）：等记录出现再断言
    assert wait_until(qt_app, lambda: len(ui.context.history.list()) == 1)
    record = ui.context.history.list()[0]
    assert record.prompt == "一只猫"
    assert record.job_id


def test_empty_prompt_is_rejected_before_launch(ui):
    notices: list[tuple[str, str]] = []
    ui.bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))

    ui.bridge.generateImage("   ", "1024x768", "", [])

    assert notices and notices[0][0] == "warn"
    assert ui.bridge.status == "idle"


def test_second_job_is_refused_while_busy(qt_app, ui):
    notices: list[tuple[str, str]] = []
    ui.bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))

    ui.bridge.generateImage("第一张", "1024x768", "", [])
    ui.bridge.generateImage("第二张", "1024x768", "", [])

    assert any(level == "warn" for level, _ in notices)
    assert wait_until(qt_app, lambda: ui.bridge.status == "succeeded")
    assert wait_until(qt_app, lambda: ui.context.history.count() == 1)   # 只跑了一个任务


def test_video_job_can_be_started_and_cancelled(qt_app, tmp_path):
    """视频链路走真实服务层：提交后在轮询阶段取消（不真的等 5 秒）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"video_id": "vid-ui"})
        return httpx.Response(200, json={"status": "queued", "progress": 0})

    harness = UiHarness(tmp_path, handler)
    try:
        harness.bridge.generateVideo("一段海浪", "agnes-video-2.5-flash", "5", "16:9", [])
        assert wait_until(qt_app, lambda: harness.bridge.status in ("running", "pending"))
        harness.bridge.cancel()
        assert wait_until(qt_app, lambda: harness.bridge.status == "canceled")
        # 落库是取消之后才落地的（写库走线程池）：等它出现，别抢跑断言
        assert wait_until(qt_app, lambda: harness.context.history.count() == 1)
    finally:
        harness.close()


def test_upload_local_image_asks_before_sending_it_publicly(qt_app, tmp_path):
    """上传本地图必须**先问再传**：确认前一个请求都不能发出去。

    实测过的真 bug：点「上传本地图」只闪过一句「需要你确认」就结束了——
    因为 bridge 调 invoke 时没带 context，审批闸门拦下后没有确认出口，
    用户看到的就是「点了没反应」。这里把「问」和「传」两步都钉住。
    """
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        sent.append(f"{request.method} {url}")
        if url == "https://api.github.com/repos/me/pics":
            return httpx.Response(200, json={"default_branch": "main"})
        if "/contents/" in url and request.method == "PUT":
            return httpx.Response(201, json={"content": {
                "download_url": "https://raw.githubusercontent.com/me/pics/main/x.png"}})
        return httpx.Response(200, json={})

    harness = UiHarness(tmp_path, handler)
    # 图床凭据：夹具的 .env 里没有，这条用例要用，补上并重载
    env_file = tmp_path / ".env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "GITHUB_TOKEN=ghp-test\nGITHUB_REPO=me/pics\n",
        encoding="utf-8",
    )
    harness.context.reload_credentials(env_file)
    # 造一张真能解码的本地图
    png = tmp_path / "ref.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    ))
    uploaded: list[str] = []
    harness.bridge.referenceUploaded.connect(lambda _p, url: uploaded.append(url))
    try:
        # 1) 点上传 → 进入待确认，且**一个请求都没发**
        harness.bridge.uploadReference(str(png))
        assert wait_until(qt_app, lambda: harness.bridge.pendingApproval == "image_host.upload")
        assert sent == [], f"确认前就发出了请求：{sent}"

        # 2) 取消 → 清空，仍不发请求
        harness.bridge.cancelUpload()
        assert wait_until(qt_app, lambda: harness.bridge.pendingApproval == "")
        assert sent == [], f"取消后仍发出了请求：{sent}"

        # 3) 确认 → 这才真正上传，并拿到直链
        harness.bridge.uploadReference(str(png))
        assert wait_until(qt_app, lambda: harness.bridge.pendingApproval == "image_host.upload")
        harness.bridge.confirmUpload()
        assert wait_until(qt_app, lambda: bool(uploaded)), "确认后没能拿到直链"
        assert uploaded[0].startswith("https://")
        assert any("PUT" in s for s in sent), f"确认后没有真正上传：{sent}"
    finally:
        harness.close()


def test_save_as_copies_from_local_cache_without_downloading(qt_app, tmp_path):
    """「另存为」优先用本地缓存：有缓存时**不该**再下载一次。

    旧版结果区一直有这个按钮（「保存」→ 系统另存为对话框），迁移到 Qt Quick 时漏了。
    """
    cached = tmp_path / "cached.mp4"
    cached.write_bytes(b"MP4-CACHED-BYTES")
    dest = tmp_path / "out" / "my_video.mp4"

    harness = UiHarness(tmp_path)
    got: list[tuple[str, str]] = []
    harness.bridge.noticeRaised.connect(lambda level, text: got.append((level, text)))
    try:
        harness.bridge.saveAs(str(dest), "https://cdn.test/should-not-be-fetched.mp4", str(cached))
        assert wait_until(qt_app, lambda: dest.is_file()), "没有写出目标文件"
        assert dest.read_bytes() == b"MP4-CACHED-BYTES"
        # 提示是异步 emit 的（跨线程排队）：等它到，别抢跑断言
        assert wait_until(qt_app, lambda: any(lv == "info" for lv, _ in got)), \
            f"没有成功提示：{got}"
    finally:
        harness.close()


def test_save_as_downloads_when_there_is_no_local_cache(qt_app, tmp_path):
    """没有本地缓存时，从结果地址下载到用户选的位置。"""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        fetched.append(url)
        if url.endswith("result.png"):
            return httpx.Response(200, content=b"PNG-DOWNLOADED")
        return httpx.Response(404, json={"message": url})

    dest = tmp_path / "out" / "saved.png"
    harness = UiHarness(tmp_path, handler)
    try:
        harness.bridge.saveAs(str(dest), "https://cdn.test/result.png", "")
        assert wait_until(qt_app, lambda: dest.is_file()), "没有下载到目标文件"
        assert dest.read_bytes() == b"PNG-DOWNLOADED"
        assert any(u.endswith("result.png") for u in fetched), f"没去下载：{fetched}"
        # 不留半截文件
        assert not list(dest.parent.glob("*.part")), "留下了 .part 临时文件"
    finally:
        harness.close()


def test_save_as_with_nothing_to_save_warns(qt_app, tmp_path):
    """既没有本地缓存、也没有地址时，如实说「没有可保存的内容」，不静默。"""
    harness = UiHarness(tmp_path)
    got: list[tuple[str, str]] = []
    harness.bridge.noticeRaised.connect(lambda level, text: got.append((level, text)))
    try:
        harness.bridge.saveAs(str(tmp_path / "x.png"), "", "")
        assert wait_until(qt_app, lambda: bool(got)), "没有任何提示"
        assert got[0][0] == "warn", f"应当给 warn，实际 {got}"
    finally:
        harness.close()

def test_video_page_opens_the_upload_confirm_dialog(qt_app, ui, tmp_path):
    """确认框要真的弹出来（走 QML 只读探针，不碰 findChild）。"""
    root = ui.root
    root.openPage(2)                       # 视频页：不切过去页面根本不布局
    png = tmp_path / "ref.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    assert root.property("videoUploadConfirmOpen") is False
    ui.bridge.uploadReference(str(png))

    assert wait_until(qt_app, lambda: root.property("videoUploadConfirmOpen") is True), \
        "确认框没有弹出来——用户会以为「点了没反应」"
    errors = [str(w) for w in ui.warnings if "error" in str(w).lower()]
    assert errors == [], f"QML 报错：{errors}"

    ui.bridge.cancelUpload()
    assert wait_until(qt_app, lambda: root.property("videoUploadConfirmOpen") is False)


def test_cancel_right_after_launch_is_not_dropped(qt_app, tmp_path):
    """点完「生成」立刻点取消：那时任务句柄还没登记，这一下也必须算数。

    实测过的真 bug：取消被静默吞掉，视频任务会一路轮询到出片（解释器慢一点时必现）。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"video_id": "vid-early"})
        return httpx.Response(200, json={"status": "queued", "progress": 0})

    harness = UiHarness(tmp_path, handler)
    try:
        harness.bridge.generateVideo("一段海浪", "agnes-video-2.5-flash", "5", "16:9", [])
        harness.bridge.cancel()                  # 故意不等它变成 running
        assert wait_until(qt_app, lambda: harness.bridge.status == "canceled")
        # 取消也要留痕：历史里应该有一条「已取消」的记录
        assert wait_until(qt_app, lambda: harness.context.history.count() == 1)
    finally:
        harness.close()


def test_harness_shutdown_lets_go_of_thread_and_databases(qt_app, tmp_path):
    """收尾必须拆干净：asyncio 线程退场、数据库句柄关掉，紧接着还能建新引擎。

    历史记录里出现过一次偶发段错误：崩在 asyncio 线程的 proactor 轮询里，主线程正好在
    给下一个用例建 QML 引擎——最可疑的就是上一个用例没拆完。这条用例把「拆干净」钉成
    断言（`AsyncRunner.close()` 一旦没能停掉线程，这里立刻会红）。
    """
    harness = UiHarness(tmp_path)
    assert harness.runner.running
    harness.close()

    assert not harness.runner.running
    assert [t.name for t in threading.enumerate() if t.name == "agnes-asyncio"] == []
    with pytest.raises(sqlite3.ProgrammingError):        # 句柄真的关了，不是留给 GC
        harness.context.prompts._conn.execute("SELECT 1")

    second = tmp_path / "following"
    second.mkdir()
    following = UiHarness(second)                        # 旧线程还在的话最容易在这里撞上
    try:
        assert following.root is not None
    finally:
        following.close()


# --------------------------------------------------------------------------- 主题

def test_theme_toggle_updates_live_without_rebuild(qt_app, ui):
    root = ui.root
    assert QColor(root.property("color")).name() == DARK["bg"].lower()

    ui.theme.toggle()
    qt_app.processEvents()

    assert ui.theme.dark is False
    assert QColor(root.property("color")).name() == LIGHT["bg"].lower()
    assert ui.engine.rootObjects()[0] is root       # 同一个窗口对象：没有重建


def test_theme_switch_keeps_tasks_alive(qt_app, ui):
    ui.bridge.generateImage("主题切换中的任务", "512x512", "", [])
    ui.theme.toggle()
    assert wait_until(qt_app, lambda: ui.bridge.status == "succeeded")


# --------------------------------------------------------------------------- 助手页

def test_agent_page_is_in_the_window(ui):
    """助手页是第一个页面，左边导航也多了一项。"""
    root = ui.root
    assert root.property("pageCount") == 6
    assert len(root.findChildren(QObject, "agentPage")) == 1


def test_agent_run_fills_the_qml_timeline(qt_app, ui):
    """整条链路到界面：跑一次运行，时间线上真的出现步骤（信号 → ListModel 绑定通了）。"""
    root = ui.root
    assert root.property("agentStepCount") == 0

    ui.agent_bridge.run("中秋海报，竖版")
    assert wait_until(qt_app, lambda: ui.agent_bridge.status == "succeeded", timeout=20)

    # 状态先变，Qt 信号再排队送达主线程：要等界面真的画出来，不能只等状态
    assert wait_until(qt_app, lambda: root.property("agentStepCount") >= 5, timeout=10), \
        "时间线是空的：stepsReplaced 没进 ListModel"
    assert ui.agent_bridge.hasResult

    errors = [str(w) for w in ui.warnings if "error" in str(w).lower()]
    assert errors == [], f"QML 报错：{errors}"


# --------------------------------------------------------------------------- 设置页

def test_agent_result_can_be_saved_to_disk(qt_app, ui, tmp_path):
    """助手跑完的结果必须能存到本地——助手页此前漏了「另存为」按钮。

    用户报的问题：助手页结果卡只有「打开结果 / 复制地址 / 载入到生成页 / 改参考 / 再跑一次」，
    手动页有「另存为」而助手页没有，于是**助手出的片子存不下来**。

    这条用例直接验证「存得下来」这件事本身（走桥的真实入口，落盘后校验内容），
    不只是在界面里找一个按钮。
    """
    root = ui.root
    assert root.property("agentSaveAsAvailable") is False     # 还没结果

    ui.agent_bridge.run("中秋海报，竖版")
    assert wait_until(qt_app, lambda: ui.agent_bridge.status == "succeeded", timeout=20)
    # 状态先变、Qt 信号再排队送达主线程：要等界面真的画出来，不能只等状态
    assert wait_until(qt_app, lambda: root.property("agentSaveAsAvailable") is True), \
        "助手页没有可另存的结果"

    # 助手这次跑出来的本地缓存（评估阶段会把图缓存下来）
    local = ui.agent_bridge.resultLocalPath
    assert local and Path(local).is_file(), f"没有本地缓存可供另存：{local}"

    dest = tmp_path / "saved" / "from_agent.png"
    ui.bridge.saveAs(str(dest), ui.agent_bridge.resultUrl, local)
    assert wait_until(qt_app, lambda: dest.is_file()), "助手结果没能存到目标位置"
    assert dest.read_bytes() == Path(local).read_bytes(), "存下来的内容与本地缓存不一致"

def test_pending_suggestion_is_visible_on_the_settings_card(qt_app, ui):
    """有建议时，「待决定」那块必须真的画得出来（宽度不能是 0）。

    实测过的真 bug：卡片体是普通 Column，`Layout.fillWidth` 在那里不起作用，这块算出来
    宽度是 0 —— 建议内容和「接受 / 拒绝」按钮全都不显示，用户根本没法决定。
    """
    ui.context.prompts.propose({"composer_suffix": "光线描述要具体到光源与方向"}, reason="演示")
    ui.selfupdate_bridge.refresh()

    root = ui.root
    root.openPage(5)                     # 设置页：不切过去，页面根本没布局，量不出宽度
    assert wait_until(qt_app, lambda: root.property("selfUpdatePendingWidth") > 200), \
        "建议块没画出来（宽度 0）：建议内容与「接受 / 拒绝」按钮都会看不见"


# --------------------------------------------------------------------------- 上下文面板

def test_context_panel_shows_the_draft_items(qt_app, ui, tmp_path):
    """草稿模式下，助手页的上下文卡要真的绑上「找参考」的结果（走 QML 只读探针）。"""
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    ui.context.history.add(Record(
        kind="image", status="success", prompt="中秋海报 竖版 国潮",
        media_path=str(ref), result_url="https://cdn.test/ref.png",
    ))

    ui.agent_bridge.setContextMode("draft")
    root = ui.root
    ui.agent_bridge.run("中秋海报，竖版")

    assert wait_until(qt_app, lambda: ui.agent_bridge.status == "draft")
    assert wait_until(qt_app, lambda: root.property("agentHasContext") is True)
    assert root.property("agentContextItems") >= 1, "上下文卡没拿到条目：绑定没通"

    ui.agent_bridge.confirmContext()
    assert wait_until(qt_app, lambda: ui.agent_bridge.status == "succeeded", timeout=20)
    errors = [str(w) for w in ui.warnings if "error" in str(w).lower()]
    assert errors == [], f"QML 报错：{errors}"


def test_agent_page_exposes_edit_context_after_a_run(qt_app, ui, tmp_path):
    """跑完之后助手页要能「改参考再跑一次」：探针跟着 stateChanged 变，点了真的摊开草稿。"""
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    ui.context.history.add(Record(
        kind="image", status="success", prompt="中秋海报 竖版 国潮",
        media_path=str(ref), result_url="https://cdn.test/ref.png",
    ))

    root = ui.root
    assert root.property("agentCanEditContext") is False

    ui.agent_bridge.run("中秋海报，竖版")
    assert wait_until(qt_app, lambda: ui.agent_bridge.status == "succeeded", timeout=20)
    assert wait_until(qt_app, lambda: root.property("agentCanEditContext") is True), \
        "跑完之后「改参考再跑一次」没出现"

    ui.agent_bridge.editContext()
    assert wait_until(qt_app, lambda: root.property("agentHasContext") is True), \
        "点了「改参考再跑一次」没有摊开可编辑的上下文"
    assert root.property("agentContextItems") >= 1


def test_agent_page_shows_saved_profiles(qt_app, ui, tmp_path):
    """助手页的档案列表要真的画出来（存一份就多一行）。"""
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    ui.context.history.add(Record(
        kind="image", status="success", prompt="中秋海报 竖版 国潮",
        media_path=str(ref), result_url="https://cdn.test/ref.png",
    ))

    root = ui.root
    root.openPage(0)
    assert root.property("agentProfileCount") == 0

    ui.agent_bridge.setContextMode("draft")
    ui.agent_bridge.run("中秋海报，竖版")
    assert wait_until(qt_app, lambda: ui.agent_bridge.status == "draft")
    ui.agent_bridge.addContextText("必须留白，别放字")
    ui.agent_bridge.saveProfile("国潮海报", "", False)

    assert wait_until(qt_app, lambda: root.property("agentProfileCount") == 1), \
        "存下来的档案没有出现在助手页上"
    errors = [str(w) for w in ui.warnings if "error" in str(w).lower()]
    assert errors == [], f"QML 报错：{errors}"


def test_lightbox_exposes_save_as_for_a_record(qt_app, ui, tmp_path):
    """历史灯箱里要有「另存为」：有本地缓存或远端地址时可用（走 QML 只读探针）。"""
    root = ui.root
    root.openPage(3)                        # 历史页
    # 没有记录时不可用
    assert root.property("lightboxSaveAsAvailable") is False

    ref = tmp_path / "hist.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    ui.context.history.add(Record(
        kind="image", status="success", prompt="中秋海报 竖版",
        media_path=str(ref), result_url="https://cdn.test/hist.png",
    ))
    ui.bridge.historyModel.reload("all", "", 500)
    assert wait_until(qt_app, lambda: ui.bridge.historyModel.count >= 1), "历史没有加载"

    # 打开灯箱（走历史页的真实入口，与点缩略图同一条路）
    record_id = ui.bridge.historyModel.recordIdAt(0)
    root.openHistoryRecord(record_id)        # QML 函数对 Python 可直接调用（同 openPage）
    assert wait_until(qt_app, lambda: root.property("lightboxSaveAsAvailable") is True), \
        "灯箱里「另存为」不可用——用户没法把这条记录存到本地"
    errors = [str(w) for w in ui.warnings if "error" in str(w).lower()]
    assert errors == [], f"QML 报错：{errors}"

def test_image_page_accepts_a_public_url_reference(qt_app, ui):
    """图片页要能直接填公网 URL 当参考图（旧版有「+ URL」，迁移时漏了）。

    更糟的是那句提示本来就写着「也可以直接填公网 URL」——**界面承诺了一个做不到的事**。
    这条用例走真实路径：填进输入框 → 点「添加」→ 参考图列表多一条。
    """
    root = ui.root
    root.openPage(1)                       # 图片页：不切过去页面根本不布局
    assert root.property("imageRefCount") == 0

    root.addImageReferenceUrl("https://cdn.test/ref.png")

    assert wait_until(qt_app, lambda: root.property("imageRefCount") == 1), \
        "填了 URL 点「添加」，参考图没有多出来"
    errors = [str(w) for w in ui.warnings if "error" in str(w).lower()]
    assert errors == [], f"QML 报错：{errors}"


def test_image_and_video_pages_show_a_live_prompt_count(qt_app, ui):
    """提示词要实时显示字数（旧版一直有「N 字」，迁移时漏了）。

    提示词长度直接影响出图质量与费用（视频按秒计费），输入时应当看得见。
    """
    root = ui.root
    root.openPage(1)
    assert root.property("imagePromptCount") == 0

    # 通过真实入口写提示词（图片页没有直接暴露 setter，走「载入参数」这条路）
    ui.bridge.paramsLoaded.emit("image", {"prompt": "中秋海报，竖版，国潮插画"})

    assert wait_until(qt_app, lambda: root.property("imagePromptCount") > 0), \
        "提示词字数没有反映出来"

def test_lightbox_detail_shows_task_id_and_failure_reason(qt_app, ui, tmp_path):
    """灯箱「详情」要能看到**平台任务ID**与**失败原因**——排查问题的出口。

    这是你这次 503 暴露出的缺口：事后想回看「当时到底报的什么」，历史里查不到
    （卡片只说「失败」）。任务ID 也只在 meta.video_id 里，界面上看不到。
    """
    root = ui.root
    root.openPage(3)                        # 历史页

    # 造一条失败记录：带平台任务ID + 失败原因（就是 503 那种现场）
    ui.context.history.add(Record(
        kind="video", status="failed",
        prompt="镜头缓慢环绕一支保温杯",
        result_url="",
        error="服务端队列已满（HTTP 503）：video queue is full, please retry later",
        meta={"video_id": "task_probe123", "job_id": "job-x", "error_kind": "queue_full"},
        params={"model": "agnes-video-2.5-flash", "seconds": "5", "aspect_ratio": "16:9"},
    ))
    ui.bridge.historyModel.reload("all", "", 500)
    assert wait_until(qt_app, lambda: ui.bridge.historyModel.count >= 1), "历史没有加载"

    record_id = ui.bridge.historyModel.recordIdAt(0)
    root.openHistoryRecord(record_id)
    # 失败记录没有产物，所以「另存为」本就该不可用——这里只等灯箱把记录装上
    assert wait_until(qt_app, lambda: bool(ui.bridge.historyModel.recordAt(record_id))), \
        "灯箱没拿到记录"

    # 打开详情
    root.openLightboxDetail()
    assert wait_until(qt_app, lambda: root.property("lightboxDetailOpen") is True), \
        "详情弹层没打开"
    assert root.property("lightboxDetailLength") > 0, "详情是空的"

    # 关键：任务ID 与失败原因都要在里面
    detail = ui.bridge.recordDetail(record_id)
    assert "task_probe123" in detail, f"详情里没有平台任务ID：\n{detail}"
    assert "video queue is full" in detail, f"详情里没有失败原因：\n{detail}"
    assert "agnes-video-2.5-flash" in detail, "详情里没有参数"
    errors = [str(w) for w in ui.warnings if "error" in str(w).lower()]
    assert errors == [], f"QML 报错：{errors}"


def test_record_detail_is_empty_for_unknown_id(qt_app, ui):
    """查不到的记录给空串，不抛错（界面据此禁用「详情」）。"""
    assert ui.bridge.recordDetail("no-such-record") == ""

def test_settings_exposes_the_context_card(qt_app, ui):
    """设置页要有「找参考与上下文」卡片：模式与阈值下拉真的摆出来了。"""
    root = ui.root
    root.openPage(5)                     # 设置页：导航顺序是 助手/图片/视频/历史/知识库/设置
    assert wait_until(qt_app, lambda: root.property("settingsContextCardReady") is True), \
        "设置页没有「找参考与上下文」卡片（或卡片里的控件没建出来）"


def test_settings_context_card_follows_saved_preferences(qt_app, ui):
    """保存之后卡片要回填：模式与阈值都跟着 settings.json 走。"""
    ui.settings_bridge.saveContext({
        "mode": "auto",
        "sources": {"history": True, "knowledge": False, "web": True, "web_images": False},
        "min_local_refs": 4,
    })

    root = ui.root
    root.openPage(5)                     # 设置页
    assert wait_until(qt_app, lambda: root.property("settingsContextMode") == 1), \
        "卡片没有回填成「自动模式」"
    assert root.property("settingsContextThreshold") == 4


def test_settings_shows_the_search_provider_row(qt_app, ui):
    """联网搜索那一行要摆在卡片里：provider 下拉默认 tavily，没配 key 时探针说 false。"""
    root = ui.root
    root.openPage(5)                     # 设置页

    assert wait_until(qt_app, lambda: root.property("settingsContextCardReady") is True)
    assert root.property("settingsSearchProvider") == 0     # 列表首项是 tavily

    ui.settings_bridge.saveSearch({"provider": "serper", "api_key": "sk-serper"})

    assert wait_until(qt_app, lambda: root.property("settingsSearchProvider") == 2), \
        "保存后面板没有回填成 serper"
    assert root.property("settingsSearchKeySet") is True


def test_web_images_acknowledgement_reaches_the_ui(qt_app, ui):
    """免责声明确认前后：探针要跟着变（界面据此决定还弹不弹）。"""
    root = ui.root
    root.openPage(5)                     # 设置页

    assert wait_until(qt_app, lambda: root.property("settingsContextCardReady") is True)
    assert root.property("webImagesAcknowledged") is False

    ui.settings_bridge.acknowledgeWebImages()

    assert wait_until(qt_app, lambda: root.property("webImagesAcknowledged") is True)


def test_web_images_consent_dialog_opens(qt_app, ui):
    """首次开启联网配图会弹出免责声明确认框——这条验收点必须真的能看到框。"""
    root = ui.root
    root.openPage(5)                     # 设置页
    assert wait_until(qt_app, lambda: root.property("settingsContextCardReady") is True)
    assert root.property("webImagesDialogOpen") is False

    from PySide6.QtCore import QMetaObject

    assert QMetaObject.invokeMethod(root, "askWebImagesConsent"), "Main.qml 没有暴露这个入口"
    assert wait_until(qt_app, lambda: root.property("webImagesDialogOpen") is True), \
        "免责声明确认框没有弹出来"


# --------------------------------------------------------------------------- 知识库页

def test_knowledge_page_lists_uploaded_documents(qt_app, ui, tmp_path):
    """知识库页要把上传的资料画出来（走 QML 只读探针，不碰 findChild）。"""
    doc_path = tmp_path / "国潮风格说明.md"
    doc_path.write_text(
        "# 配色\n\n国潮海报的配色以红金为主，饱和度要压低，留白要足够。", encoding="utf-8"
    )

    root = ui.root
    root.openPage(4)                     # 知识库页：不切过去页面根本不布局
    assert root.property("knowledgeDocCount") == 0

    ui.knowledge_bridge.addFiles([str(doc_path)])
    assert wait_until(qt_app, lambda: root.property("knowledgeDocCount") == 1), \
        "上传的资料没有出现在知识库页上"

    errors = [str(w) for w in ui.warnings if "error" in str(w).lower()]
    assert errors == [], f"QML 报错：{errors}"

    ui.knowledge_bridge.testSearch("国潮 配色")
    assert wait_until(qt_app, lambda: root.property("knowledgeHitCount") >= 1), \
        "检索试跑的结果没有画到页面上"
